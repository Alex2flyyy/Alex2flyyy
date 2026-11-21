#!/usr/bin/env python
"""Release-readiness audit for loop-beat reuse. Local only, zero spend.

Renders the same concept twice — once with ``reuse_loop_clip`` on, once off —
then measures the things the optimisation could plausibly have broken:

  * the last-to-first seam, sampled across a window of frames rather than one
    pair, and reported in frames and milliseconds at the project frame rate,
    because a viewer at normal speed sees ~33ms per frame;
  * caption card timing against the real rendered duration;
  * the audio fade at both ends;
  * byte-level determinism across reruns.

Everything here uses the `synth` providers. No API token is read, no provider
is called, nothing is scheduled or published.

    .venv/bin/python scripts/qa_audit.py [--out DIR] [--seed N]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pawparty.config import load_channel, load_settings  # noqa: E402
from pawparty.media import ffmpeg  # noqa: E402
from pawparty.pipeline import Pipeline  # noqa: E402


# --------------------------------------------------------------------------- #
# Measurement helpers
# --------------------------------------------------------------------------- #
def ssim(a: Path, b: Path) -> float:
    """Structural similarity between two stills. 1.0 is identical."""
    proc = subprocess.run(
        [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-y",
         "-i", str(a), "-i", str(b), "-lavfi", "ssim", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"All:([0-9.]+)", proc.stderr)
    if not match:
        raise RuntimeError(f"ffmpeg ssim produced no score:\n{proc.stderr[-600:]}")
    return float(match.group(1))


def segment_levels(video: Path, start: float, duration: float) -> tuple[float, float]:
    """``(mean_dbfs, peak_dbfs)`` for an audio segment. -inf when silent."""
    proc = subprocess.run(
        [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-y",
         "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
         "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )

    def grab(key: str) -> float:
        match = re.search(rf"{key}:\s*(-?[0-9.]+|-inf) dB", proc.stderr)
        if not match:
            return float("-inf")
        return float("-inf") if match.group(1) == "-inf" else float(match.group(1))

    return grab("mean_volume"), grab("max_volume")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #
@dataclass
class Variant:
    label: str
    reuse: bool
    final: Path = field(default=Path())
    manifest: dict = field(default_factory=dict)
    workspace: Path = field(default=Path())


def render(label: str, *, reuse: bool, seed: int, out: Path) -> Variant:
    """Render one variant into its own isolated workspace."""
    workspace = out / f"ws_{label}"
    shutil.rmtree(workspace, ignore_errors=True)
    os.environ["PAWPARTY_WORKSPACE"] = str(workspace)

    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    settings.reuse_loop_clip = reuse
    settings.subtitles.min_duration_s = 0.0  # always exercise burn-in

    pipeline = Pipeline(settings, load_channel(settings))
    result = pipeline.run_batch(count=1, seed=seed, concurrency=1)
    if result.failed:
        raise SystemExit(f"[{label}] render failed: {result.failed[0].error}")

    job = result.succeeded[0]
    manifest_path = pipeline.workspace.manifest_path(job.concept.id, job.date)
    return Variant(
        label=label, reuse=reuse,
        final=Path(job.final_path),
        manifest=json.loads(manifest_path.read_text(encoding="utf-8")),
        workspace=workspace,
    )


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def safe_frame(video: Path, output: Path, at_s: float, step: float) -> Path:
    """Extract a frame, backing off when the seek lands past the last one.

    Seeking to within a frame of the end often yields no frame at all, so a
    naive extraction at ``duration - 1/fps`` silently produces nothing.
    """
    for attempt in range(4):
        target = max(0.0, at_s - attempt * step)
        with contextlib.suppress(Exception):
            ffmpeg.extract_frame(video, output, target)
            if output.exists() and output.stat().st_size > 0:
                return output
    raise RuntimeError(f"could not extract a frame near {at_s:.3f}s of {video.name}")


def seam_profile(video: Path, out: Path, tag: str, fps: int, window: int = 6) -> dict:
    """Compare the closing frames against the opening frames.

    A single last-vs-first pair is a weak signal: it can be flattered by a
    fade. Sampling a window shows whether the *approach* to the loop point
    converges, which is what a viewer actually perceives at playback speed.
    """
    duration = ffmpeg.probe(video).duration_s
    step = 1.0 / fps
    # Stay 1.5 frames clear of the end so the final seek always lands on video.
    tail_anchor = duration - 1.5 * step
    scores = []
    for i in range(window):
        head = safe_frame(video, out / f"{tag}_h{i}.png", i * step, step)
        tail = safe_frame(video, out / f"{tag}_t{i}.png",
                          max(0.0, tail_anchor - (window - 1 - i) * step), step)
        scores.append(round(ssim(head, tail), 4))

    first = safe_frame(video, out / f"{tag}_first.png", 0.0, step)
    last = safe_frame(video, out / f"{tag}_last.png", tail_anchor, step)
    return {
        "duration_s": round(duration, 3),
        "fps": fps,
        "frame_ms": round(1000 / fps, 1),
        "window_frames": window,
        "window_span_ms": round(window * 1000 / fps, 1),
        "last_vs_first": round(ssim(first, last), 4),
        "window_scores": scores,
        "window_mean": round(statistics.mean(scores), 4),
    }


def caption_check(manifest: dict, duration: float) -> dict:
    cards = manifest.get("copy", {}).get("subtitle_lines") or []
    issues = []
    for i, card in enumerate(cards):
        if card["start"] < 0:
            issues.append(f"card {i} starts before zero")
        if card["end"] > duration + 0.01:
            issues.append(f"card {i} ends at {card['end']}s, past the {duration:.2f}s video")
        if card["end"] <= card["start"]:
            issues.append(f"card {i} has no duration")
    for a, b in zip(cards, cards[1:]):
        if b["start"] < a["end"] - 0.01:
            issues.append(f"cards overlap: {a['text']!r} / {b['text']!r}")
    hook = [c for c in cards if c.get("emphasis") == "hook"]
    if hook and hook[0]["start"] > 0.01:
        issues.append(f"hook card starts at {hook[0]['start']}s, not frame one")
    return {
        "cards": len(cards),
        "hook_at_zero": bool(hook) and hook[0]["start"] <= 0.01,
        "last_card_end_s": round(max((c["end"] for c in cards), default=0.0), 2),
        "headroom_s": round(duration - max((c["end"] for c in cards), default=0.0), 2),
        "issues": issues,
        "detail": [
            {"start": c["start"], "end": c["end"], "emphasis": c["emphasis"], "text": c["text"]}
            for c in cards
        ],
    }


def audio_check(video: Path, fade_in: float, fade_out: float) -> dict:
    """Confirm the bed actually fades at both ends.

    The measurement window has to be *narrower* than the fade, or the mean is
    dominated by the loud samples at the start of the window and a working fade
    reads as no fade at all. A third of the fade length resolves it, and the
    trailing peak is what matters for an audible click on loop restart.
    """
    info = ffmpeg.probe(video)
    duration = info.duration_s
    head_w = max(0.02, fade_in / 3)
    tail_w = max(0.02, fade_out / 3)

    head_mean, head_peak = segment_levels(video, 0.0, head_w)
    mid_mean, mid_peak = segment_levels(video, duration / 2, 0.25)
    tail_mean, tail_peak = segment_levels(video, max(0.0, duration - tail_w), tail_w)

    def depth(edge: float) -> float | None:
        if edge == float("-inf"):
            return None
        return round(mid_mean - edge, 2)

    return {
        "has_audio": info.has_audio,
        "window_ms": {"head": round(head_w * 1000), "tail": round(tail_w * 1000)},
        "head_mean_dbfs": head_mean,
        "mid_mean_dbfs": mid_mean,
        "tail_mean_dbfs": tail_mean,
        "tail_peak_dbfs": tail_peak,
        "fade_in_depth_db": depth(head_mean),
        "fade_out_depth_db": depth(tail_mean),
        # A loop restart is audible if the tail is still near full level.
        "fade_out_ok": tail_mean != float("-inf") and (mid_mean - tail_mean) >= 6.0,
        "fade_in_ok": head_mean != float("-inf") and (mid_mean - head_mean) >= 3.0,
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=Path(os.environ.get("PAWPARTY_QA_OUT", "/tmp/pawparty-qa")))
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    base_render = load_settings(PROJECT_ROOT / "config" / "settings.yaml").render
    fps = base_render.fps

    report: dict = {"seed": args.seed, "variants": {}}

    for label, reuse in (("after", True), ("before", False)):
        variant = render(label, reuse=reuse, seed=args.seed, out=out)
        duration = ffmpeg.probe(variant.final).duration_s
        info = ffmpeg.probe(variant.final)
        beats = variant.manifest["concept"]["beats"]

        report["variants"][label] = {
            "reuse_loop_clip": reuse,
            "concept_id": variant.manifest["concept"]["id"],
            "title": variant.manifest["concept"]["title"],
            "beats": len(beats),
            "derived_beats": sum(1 for b in beats if b["derived_from"]),
            "clips_stage": variant.manifest["stages"]["clips"]["detail"],
            "stills_stage": variant.manifest["stages"]["stills"]["detail"],
            "file": {
                "duration_s": round(info.duration_s, 3),
                "resolution": f"{info.width}x{info.height}",
                "fps": round(info.fps, 2),
                "size_mb": round(info.size_mb, 2),
                "video_codec": info.video_codec,
                "audio_codec": info.audio_codec,
            },
            "qc": variant.manifest["qc_report"],
            "seam": seam_profile(variant.final, out, label, fps),
            "captions": caption_check(variant.manifest, duration),
            "audio": audio_check(
                variant.final,
                base_render.audio_fade_in_s,
                base_render.audio_fade_out_s,
            ),
        }
        shutil.copy2(variant.final, out / f"preview-{label}.mp4")

    # --- determinism: render `after` again and compare bytes --------------- #
    rerun = render("after_rerun", reuse=True, seed=args.seed, out=out)
    first = out / "preview-after.mp4"
    report["determinism"] = {
        "final_mp4_sha256_run1": sha256(first),
        "final_mp4_sha256_run2": sha256(rerun.final),
        "identical": sha256(first) == sha256(rerun.final),
        "concept_signature_run1": report["variants"]["after"]["concept_id"],
        "concept_signature_run2": rerun.manifest["concept"]["id"],
    }

    (out / "qa_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nartifacts: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
