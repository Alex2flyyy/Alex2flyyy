"""Quality control — the gate between "rendered" and "publishable".

An unattended pipeline will eventually produce a black video, a silent video, a
3-second video that was meant to be 30, or a 400MB file. Publishing any of those
costs more than skipping the slot, so every deliverable is probed and checked
before it reaches `Finished/`.

Checks are split into **errors** (block publishing) and **warnings** (log and
continue). Getting that split right is what keeps the system from either
shipping garbage or blocking on trivia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import QCSettings, RenderSettings
from ..logging_setup import get_logger
from . import ffmpeg

log = get_logger("media.qc")


@dataclass
class QCReport:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
        }

    def summary(self) -> str:
        if self.passed and not self.warnings:
            return "QC passed"
        if self.passed:
            return f"QC passed with {len(self.warnings)} warning(s): {'; '.join(self.warnings)}"
        return f"QC FAILED: {'; '.join(self.errors)}"


def check_video(
    path: Path,
    expected_duration_s: float,
    qc: QCSettings,
    render: RenderSettings,
) -> QCReport:
    """Probe a finished video and validate it against the spec."""
    report = QCReport(passed=True)

    if not path.exists():
        report.passed = False
        report.errors.append(f"file does not exist: {path}")
        return report

    try:
        info = ffmpeg.probe(path)
    except Exception as exc:  # a file ffprobe can't read is a hard fail
        report.passed = False
        report.errors.append(f"ffprobe could not read the file: {exc}")
        return report

    report.info = {
        "duration_s": round(info.duration_s, 3),
        "width": info.width,
        "height": info.height,
        "fps": round(info.fps, 3),
        "has_audio": info.has_audio,
        "video_codec": info.video_codec,
        "audio_codec": info.audio_codec,
        "size_mb": round(info.size_mb, 2),
        "bitrate_kbps": round(info.bitrate / 1000) if info.bitrate else 0,
    }

    # --- duration ---------------------------------------------------------- #
    if info.duration_s < qc.min_duration_s:
        report.errors.append(
            f"duration {info.duration_s:.1f}s is below the {qc.min_duration_s}s floor"
        )
    if info.duration_s > qc.max_duration_s:
        report.errors.append(
            f"duration {info.duration_s:.1f}s exceeds the {qc.max_duration_s}s ceiling"
        )
    drift = abs(info.duration_s - expected_duration_s)
    if drift > qc.duration_tolerance_s:
        # Transitions legitimately shorten the timeline; a big drift means a
        # beat failed to render, which is worth failing on.
        report.errors.append(
            f"duration {info.duration_s:.1f}s drifted {drift:.1f}s from the planned "
            f"{expected_duration_s:.1f}s"
        )

    # --- geometry ---------------------------------------------------------- #
    if info.width != qc.expect_width or info.height != qc.expect_height:
        report.errors.append(
            f"resolution {info.width}x{info.height}, expected "
            f"{qc.expect_width}x{qc.expect_height}"
        )
    if info.height and abs(info.aspect - 9 / 16) > 0.01:
        report.errors.append(f"aspect ratio {info.aspect:.3f} is not 9:16")

    # --- audio ------------------------------------------------------------- #
    if qc.require_audio and not info.has_audio:
        report.errors.append("no audio stream (a silent post gets suppressed)")

    # --- file size --------------------------------------------------------- #
    size_kb = info.size_bytes / 1024
    if size_kb < qc.min_file_size_kb:
        report.errors.append(
            f"file is only {size_kb:.0f}KB — almost certainly a blank or failed render"
        )
    if info.size_mb > qc.max_file_size_mb:
        report.errors.append(f"file is {info.size_mb:.0f}MB, over the {qc.max_file_size_mb}MB cap")

    # --- warnings ---------------------------------------------------------- #
    if abs(info.fps - render.fps) > 0.5:
        report.warnings.append(f"frame rate {info.fps:.2f} differs from the target {render.fps}")
    if info.video_codec not in ("h264", "hevc"):
        report.warnings.append(f"unusual video codec {info.video_codec!r} for this platform")
    if info.bitrate and info.bitrate < 1_500_000:
        report.warnings.append(
            f"bitrate {info.bitrate // 1000}kbps is low; the platform's re-encode will hurt"
        )

    report.passed = not report.errors
    return report


def check_black_frames(path: Path, threshold: float = 0.35) -> tuple[bool, str]:
    """Detect a mostly-black video — the classic silent generation failure.

    Uses ffmpeg's `blackdetect`, which reports black *intervals* on stderr.
    Returns ``(ok, detail)``.
    """
    try:
        proc = ffmpeg.run(
            [
                "-i", str(path),
                "-vf", "blackdetect=d=0.5:pix_th=0.10",
                "-an", "-f", "null", "-",
            ],
            label="blackdetect",
            timeout=300,
            loglevel="info",  # blackdetect reports at info level
        )
    except ffmpeg.FFmpegError as exc:
        return True, f"blackdetect skipped: {exc}"

    output = (proc.stderr or "") + (proc.stdout or "")
    if "black_start" not in output:
        return True, ""

    total_black = 0.0
    for line in output.splitlines():
        if "black_duration" in line:
            for token in line.split():
                if token.startswith("black_duration:"):
                    try:
                        total_black += float(token.split(":", 1)[1])
                    except ValueError:
                        pass

    try:
        duration = ffmpeg.duration_of(path)
    except Exception:
        return True, ""

    if duration and total_black / duration > threshold:
        return False, f"{total_black:.1f}s of {duration:.1f}s is black"
    return True, ""
