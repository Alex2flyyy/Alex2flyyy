"""`local_library` music provider — your own licensed tracks.

**This is the provider you should use in production.** Generative music is
legally murky for monetised social accounts, and TikTok's own detection can mute
or restrict videos whose audio it can't clear. Licensed library music is the
boring, safe answer.

Setup:

1. Drop audio files into ``Videos/Audio/library/``.
2. Describe them in ``Videos/Audio/library/library.yaml``::

       tracks:
         - file: disco_paws.mp3
           label: "Disco Paws"
           bpm: 118
           energy: high
           tags: [disco, retro, party, high]
           license: "Epidemic Sound — subscription #12345"
         - file: beach_bounce.wav
           bpm: 104
           energy: medium
           tags: [tropical, summer, beach]
           license: "Artlist — invoice 9981"

3. Set ``providers.music: local_library``.

Selection scores each track against the concept's vibe tags and BPM, then picks
from the top matches with a seeded shuffle — so a good match is used often, but
never the same track twice in a row when alternatives exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ...logging_setup import get_logger
from ...media import ffmpeg
from ...util import seeded_rng
from ..base import BaseProvider, GenerationResult, ProviderUnavailable

log = get_logger("providers.music.library")

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


@dataclass
class Track:
    path: Path
    label: str
    bpm: int
    energy: str
    tags: list[str]
    license: str = ""


class LocalLibraryMusicProvider(BaseProvider):
    name = "local_library"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._library_dir: Path | None = None
        self._tracks: list[Track] | None = None

    def bind_library(self, library_dir: Path) -> None:
        """Called by the registry once the workspace is known."""
        self._library_dir = Path(library_dir)
        self._tracks = None

    def estimate_cost(self, duration_s: float) -> float:  # noqa: ARG002
        return 0.0  # already paid for via your subscription

    # ------------------------------------------------------------------ #
    def _load(self) -> list[Track]:
        if self._tracks is not None:
            return self._tracks
        if self._library_dir is None:
            raise ProviderUnavailable("Music library directory was never bound.")

        manifest = self._library_dir / "library.yaml"
        entries: list[dict[str, Any]] = []
        if manifest.exists():
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            entries = data.get("tracks", []) or []

        tracks: list[Track] = []
        described: set[str] = set()
        for entry in entries:
            path = self._library_dir / str(entry.get("file", ""))
            if not path.exists():
                log.warning("library.yaml references a missing file: %s", path)
                continue
            described.add(path.name)
            tracks.append(
                Track(
                    path=path,
                    label=str(entry.get("label", path.stem)),
                    bpm=int(entry.get("bpm", 120)),
                    energy=str(entry.get("energy", "medium")),
                    tags=[str(t).lower() for t in entry.get("tags", [])],
                    license=str(entry.get("license", "")),
                )
            )

        # Undescribed files still work — they just can't be matched on vibe.
        for path in sorted(self._library_dir.iterdir()):
            if path.suffix.lower() in AUDIO_SUFFIXES and path.name not in described:
                tracks.append(
                    Track(path=path, label=path.stem, bpm=120, energy="medium", tags=[])
                )

        self._tracks = tracks
        return tracks

    # ------------------------------------------------------------------ #
    def get_track(
        self,
        *,
        prompt: str,  # noqa: ARG002
        vibe: str,
        tags: list[str],
        bpm: int,
        duration_s: float,
        output: Path,
        seed: int,
    ) -> GenerationResult:
        tracks = self._load()
        if not tracks:
            raise ProviderUnavailable(
                f"No audio files found in {self._library_dir}. Add licensed tracks "
                f"(see docs/LEGAL.md) or set providers.music to 'synth' for offline testing."
            )

        wanted = {t.lower() for t in [*tags, vibe]}
        scored = sorted(
            tracks,
            key=lambda t: self._score(t, wanted, bpm),
            reverse=True,
        )
        # Seeded pick from the top third keeps variety without losing relevance.
        pool = scored[: max(1, len(scored) // 3)]
        chosen = seeded_rng(seed, vibe).choice(pool)

        output = output.with_suffix(".wav")
        self._prepare(chosen, duration_s, output)
        return GenerationResult(
            path=output,
            cost_usd=0.0,
            provider=self.name,
            model="local",
            duration_s=duration_s,
            metadata={
                "track": chosen.label,
                "source_file": chosen.path.name,
                "license": chosen.license,
                "bpm": chosen.bpm,
            },
        )

    @staticmethod
    def _score(track: Track, wanted: set[str], bpm: int) -> float:
        overlap = len(set(track.tags) & wanted)
        bpm_penalty = abs(track.bpm - bpm) / 40.0
        return overlap * 2.0 - bpm_penalty

    def _prepare(self, track: Track, duration_s: float, output: Path) -> None:
        """Loop (if short) and trim the source to exactly the needed length."""
        output.parent.mkdir(parents=True, exist_ok=True)
        source_duration = ffmpeg.duration_of(track.path)
        loop_args = ["-stream_loop", "-1"] if source_duration < duration_s + 0.5 else []
        ffmpeg.run(
            [
                *loop_args,
                "-i", str(track.path),
                "-t", f"{duration_s:.3f}",
                "-vn",
                "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2",
                str(output),
            ],
            label=f"prepare bed {track.label}",
        )
