"""`synth` music provider — a tiny pure-Python software synth.

Generates a royalty-free, loopable, on-tempo bed using nothing but the standard
library (`wave` + `math`). It will not chart, but it is *musically correct*:
right BPM, right length, clean loop point, dance-friendly downbeat. That's
enough to validate the mix, the ducking, the fades and the cut timing before you
license real music.

Why a hand-rolled synth instead of an ffmpeg `aevalsrc` expression: expression
strings need comma-escaping inside filter arguments, which turns into an
unreadable, fragile mess. 90 lines of Python is clearer and testable.

**For publishing, use real licensed music.** See docs/LEGAL.md — TikTok's
Commercial Music Library or a subscription service (Epidemic Sound, Artlist,
Soundstripe). Point the `local_library` provider at the files you licensed.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

from ...util import seeded_rng
from ..base import BaseProvider, GenerationResult

SAMPLE_RATE = 44100

#: Scale degrees (semitone offsets) per energy level. Major pentatonic reads as
#: "happy" across cultures, which is exactly the brief.
_SCALES = {
    "low": [0, 3, 5, 7, 10],       # minor pentatonic — mellow
    "medium": [0, 2, 4, 7, 9],     # major pentatonic
    "high": [0, 2, 4, 7, 9, 12],   # major pentatonic + octave
}

#: Root notes by vibe keyword, so different vibes actually sound different.
_ROOTS = {
    "disco": 55.00, "funk": 58.27, "hiphop": 49.00, "chiptune": 65.41,
    "tropical": 61.74, "cinematic": 51.91, "lofi": 46.25, "edm": 55.00,
}


class SynthMusicProvider(BaseProvider):
    """Renders a WAV bed at the concept's BPM."""

    name = "synth"

    def estimate_cost(self, duration_s: float) -> float:  # noqa: ARG002
        return 0.0

    def get_track(
        self,
        *,
        prompt: str,  # noqa: ARG002 - no text conditioning offline
        vibe: str,
        tags: list[str],
        bpm: int,
        duration_s: float,
        output: Path,
        seed: int,
    ) -> GenerationResult:
        output = output.with_suffix(".wav")
        output.parent.mkdir(parents=True, exist_ok=True)

        rng = seeded_rng(seed, vibe, bpm)
        energy = _energy_from(tags)
        scale = _SCALES[energy]
        root = _root_for(vibe, tags)
        # Two bars of pattern, then repeat — gives a real loop point.
        pattern = [rng.choice(scale) for _ in range(8)]

        samples = self._render(
            duration_s=duration_s,
            bpm=max(60, min(180, bpm)),
            root=root,
            pattern=pattern,
            energy=energy,
        )
        self._write_wav(output, samples)

        return GenerationResult(
            path=output,
            cost_usd=0.0,
            provider=self.name,
            model="pure-python-synth",
            duration_s=duration_s,
            metadata={"placeholder": True, "bpm": bpm, "vibe": vibe, "energy": energy},
        )

    # ------------------------------------------------------------------ #
    def _render(
        self,
        *,
        duration_s: float,
        bpm: int,
        root: float,
        pattern: list[int],
        energy: str,
    ) -> array.array:
        total = int(duration_s * SAMPLE_RATE)
        beat_s = 60.0 / bpm
        step_s = beat_s / 2.0            # eighth notes
        bass_s = beat_s * 2.0            # half-note bass movement

        gain = {"low": 0.5, "medium": 0.7, "high": 0.85}[energy]
        out = array.array("h", bytes(total * 2))

        for i in range(total):
            t = i / SAMPLE_RATE

            # --- melody: plucked arpeggio with exponential decay -----------
            step_index = int(t / step_s)
            step_phase = t - step_index * step_s
            semitone = pattern[step_index % len(pattern)] + 24  # two octaves up
            freq = root * (2 ** (semitone / 12.0))
            env = math.exp(-6.0 * step_phase)
            melody = math.sin(2 * math.pi * freq * t) * env * 0.32
            # A little second harmonic keeps it from sounding like a test tone.
            melody += math.sin(4 * math.pi * freq * t) * env * 0.10

            # --- bass: root/fifth alternation ------------------------------
            bass_index = int(t / bass_s)
            bass_semi = 0 if bass_index % 2 == 0 else 7
            bass_freq = root * (2 ** (bass_semi / 12.0))
            bass_env = math.exp(-1.6 * (t - bass_index * bass_s))
            bass = math.sin(2 * math.pi * bass_freq * t) * bass_env * 0.30

            # --- kick: pitch-dropping sine on every beat -------------------
            beat_phase = t % beat_s
            kick_env = math.exp(-9.0 * beat_phase)
            kick_freq = 48 + 120 * math.exp(-28.0 * beat_phase)
            kick = math.sin(2 * math.pi * kick_freq * beat_phase) * kick_env * 0.45

            # --- hat: deterministic pseudo-noise on the offbeat ------------
            hat = 0.0
            if energy != "low":
                off_phase = (t + beat_s / 2) % beat_s
                if off_phase < 0.05:
                    noise = math.sin(i * 12.9898) * 43758.5453
                    hat = (noise - math.floor(noise) - 0.5) * math.exp(-70 * off_phase) * 0.18

            value = (melody + bass + kick + hat) * gain
            # Soft clip — keeps the bed from ever hard-clipping into the mix.
            value = math.tanh(value * 1.15)
            out[i] = int(max(-1.0, min(1.0, value)) * 30000)

        self._apply_edges(out, total)
        return out

    @staticmethod
    def _apply_edges(samples: array.array, total: int) -> None:
        """20ms fades at both ends so the bed never clicks on loop or cut."""
        edge = min(int(0.02 * SAMPLE_RATE), total // 4)
        for i in range(edge):
            factor = i / edge
            samples[i] = int(samples[i] * factor)
            samples[total - 1 - i] = int(samples[total - 1 - i] * factor)

    @staticmethod
    def _write_wav(path: Path, samples: array.array) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(samples.tobytes())


def _energy_from(tags: list[str]) -> str:
    lowered = {t.lower() for t in tags}
    if lowered & {"high", "energetic", "party", "hype", "edm"}:
        return "high"
    if lowered & {"low", "mellow", "calm", "lofi", "cozy"}:
        return "low"
    return "medium"


def _root_for(vibe: str, tags: list[str]) -> float:
    haystack = " ".join([vibe, *tags]).lower()
    for key, freq in _ROOTS.items():
        if key in haystack:
            return freq
    return 55.00  # A1
