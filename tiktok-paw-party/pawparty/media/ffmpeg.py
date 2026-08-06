"""Thin, honest wrapper around ffmpeg/ffprobe.

Design rules:

* Build **argument lists**, never shell strings. No quoting bugs, no injection
  surface from a caption someone typed.
* Every failure raises :class:`FFmpegError` carrying the last lines of stderr,
  because ffmpeg's actual error is always in the tail and nowhere else.
* Probe results are typed, so the QC stage isn't parsing JSON by hand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..logging_setup import get_logger

log = get_logger("media.ffmpeg")

#: How much stderr to keep in an exception message.
_ERROR_TAIL_LINES = 18


class FFmpegError(RuntimeError):
    """ffmpeg or ffprobe exited non-zero."""

    def __init__(self, command: Sequence[str], returncode: int, stderr: str) -> None:
        tail = "\n".join(stderr.strip().splitlines()[-_ERROR_TAIL_LINES:])
        super().__init__(
            f"ffmpeg exited {returncode}\n"
            f"  command: {' '.join(str(c) for c in command[:12])}"
            f"{' …' if len(command) > 12 else ''}\n"
            f"  stderr tail:\n{tail}"
        )
        self.command = list(command)
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class MediaInfo:
    """Everything QC needs to know about a rendered file."""

    path: Path
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str
    size_bytes: int
    bitrate: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "ffmpeg not found on PATH. Install it first:\n"
            "  macOS:  brew install ffmpeg\n"
            "  Debian: sudo apt-get install -y ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg"
        )
    return path


def ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise FileNotFoundError("ffprobe not found on PATH (it ships with ffmpeg).")
    return path


def is_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def version() -> str:
    out = subprocess.run(
        [ffmpeg_path(), "-version"], capture_output=True, text=True, check=False
    )
    return out.stdout.splitlines()[0] if out.stdout else "unknown"


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def run(
    args: Sequence[str],
    *,
    timeout: int = 900,
    label: str = "",
    loglevel: str = "error",
) -> subprocess.CompletedProcess:
    """Run ffmpeg with ``args`` (without the binary itself or `-y`).

    ``loglevel`` is raised to ``info`` by callers that need to read ffmpeg's
    analysis output (e.g. ``blackdetect``), which is suppressed at ``error``.
    """
    command = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", loglevel, "-y", *map(str, args)]
    log.debug("ffmpeg %s", label or " ".join(command[6:14]))
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(command, -1, f"timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise FFmpegError(command, proc.returncode, proc.stderr)
    return proc


def probe(path: Path | str) -> MediaInfo:
    """ffprobe a media file into a :class:`MediaInfo`."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Cannot probe missing file: {path}")

    command = [
        ffprobe_path(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise FFmpegError(command, proc.returncode, proc.stderr)

    data: dict[str, Any] = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video.get("duration") or 0.0)

    return MediaInfo(
        path=path,
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fps(video.get("r_frame_rate", "0/1")),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name", "")),
        audio_codec=str(audio.get("codec_name", "")) if audio else "",
        size_bytes=path.stat().st_size,
        bitrate=int(fmt.get("bit_rate") or 0),
    )


def _parse_fps(rate: str) -> float:
    try:
        num, _, den = rate.partition("/")
        denominator = float(den or 1)
        return float(num) / denominator if denominator else 0.0
    except (TypeError, ValueError):
        return 0.0


def duration_of(path: Path | str) -> float:
    return probe(path).duration_s


# --------------------------------------------------------------------------- #
# Small reusable operations
# --------------------------------------------------------------------------- #
def extract_frame(source: Path, output: Path, at_s: float = 0.0) -> Path:
    """Pull a single frame as a PNG (covers, loop-matching, thumbnails)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["-ss", f"{max(0.0, at_s):.3f}", "-i", str(source), "-frames:v", "1", str(output)],
        label=f"extract_frame {output.name}",
    )
    return output


def concat_demux(clips: Sequence[Path], output: Path, work_dir: Path) -> Path:
    """Lossless concat via the demuxer. Requires identical codec parameters."""
    work_dir.mkdir(parents=True, exist_ok=True)
    listing = work_dir / "concat.txt"
    listing.write_text(
        "\n".join(f"file '{Path(c).resolve().as_posix()}'" for c in clips) + "\n",
        encoding="utf-8",
    )
    run(
        ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(output)],
        label=f"concat {len(clips)} clips",
    )
    return output


def escape_filter_text(text: str) -> str:
    """Escape a string for use inside a `drawtext` filter value."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")   # a real apostrophe avoids the quoting minefield
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def escape_path_for_filter(path: Path | str) -> str:
    """Escape a filesystem path for filters that take one (subtitles=…)."""
    text = str(path)
    return text.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
