"""Video assembly: normalise beats, apply transitions, close the loop, mux.

Pipeline inside this module::

    raw clips ──▶ prepare_beat()   normalise + camera move + exact duration
              ──▶ stitch()         cuts and cross-transitions between beats
              ──▶ close_loop()     make the last frame rhyme with the first
              ──▶ mux()            add the music bed, burn subtitles, encode

Everything is re-entrant: each step writes a named file and skips the work when
that file already exists and is newer than its inputs, so a failed run resumes
instead of restarting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..config import RenderSettings, Settings
from ..logging_setup import get_logger
from ..models import Beat, Concept
from . import ffmpeg
from .motion import build_beat_chain

log = get_logger("media.assemble")

#: Transition id -> (xfade name, duration seconds). `cut` is handled separately.
TRANSITIONS: dict[str, tuple[str, float]] = {
    "cut": ("", 0.0),
    "crossfade": ("fade", 0.28),
    "whip_pan": ("slideleft", 0.20),
    "whip_pan_right": ("slideright", 0.20),
    "zoom_blur": ("smoothup", 0.26),
    "flash": ("fadewhite", 0.18),
    "circle_open": ("circleopen", 0.30),
    "wipe_up": ("wipeup", 0.22),
    "pixelize": ("pixelize", 0.24),
    "dissolve": ("dissolve", 0.30),
}


def _is_fresh(target: Path, sources: Sequence[Path]) -> bool:
    """True when ``target`` exists and post-dates every source."""
    if not target.exists() or target.stat().st_size == 0:
        return False
    target_mtime = target.stat().st_mtime
    return all(not s.exists() or s.stat().st_mtime <= target_mtime for s in sources)


# --------------------------------------------------------------------------- #
# Step 1 — per beat
# --------------------------------------------------------------------------- #
def prepare_beat(
    raw_clip: Path,
    beat: Beat,
    render: RenderSettings,
    output: Path,
    *,
    intensity: float = 1.0,
) -> Path:
    """Normalise one raw clip to spec and apply its camera move.

    Duration handling matters: video models return whatever length they support,
    so we *always* trim, and if the clip came back short we slow it down to fit
    rather than freeze-framing the tail (a freeze on a dance clip is obvious).
    """
    if _is_fresh(output, [raw_clip]):
        log.debug("beat %s already prepared", beat.slug)
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(raw_clip)
    target = beat.duration_s

    chain = build_beat_chain(render, beat.camera, target, intensity=intensity)

    if info.duration_s > 0 and info.duration_s < target - 0.15:
        # Short clip: retime with setpts so movement stays continuous.
        factor = target / info.duration_s
        chain = f"setpts={factor:.5f}*PTS,{chain}"
        log.debug("beat %s retimed x%.2f (%.2fs -> %.2fs)",
                  beat.slug, factor, info.duration_s, target)

    ffmpeg.run(
        [
            "-i", str(raw_clip),
            "-an",
            "-vf", chain,
            "-t", f"{target:.3f}",
            "-r", str(render.fps),
            "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
            "-pix_fmt", render.pixel_format,
            str(output),
        ],
        label=f"prepare {beat.slug}",
    )
    return output


# --------------------------------------------------------------------------- #
# Step 2 — stitch
# --------------------------------------------------------------------------- #
def stitch(
    clips: Sequence[Path],
    beats: Sequence[Beat],
    render: RenderSettings,
    output: Path,
    work_dir: Path,
) -> Path:
    """Join prepared beats, honouring each beat's ``transition_in``.

    When every transition is a hard cut we use the concat demuxer (stream copy,
    instant, lossless). As soon as one beat wants a real transition we build a
    single ``xfade`` chain — chaining xfades in one graph is markedly faster and
    higher quality than re-encoding pairwise.
    """
    clips = [Path(c) for c in clips]
    if not clips:
        raise ValueError("stitch() needs at least one clip")
    if _is_fresh(output, clips):
        log.debug("stitched video already present")
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        ffmpeg.run(["-i", str(clips[0]), "-c", "copy", str(output)], label="stitch single")
        return output

    wants_transition = any(
        TRANSITIONS.get(b.transition_in, ("", 0.0))[0] for b in beats[1:]
    )
    if not wants_transition:
        return ffmpeg.concat_demux(clips, output, work_dir)

    return _stitch_with_xfade(clips, beats, render, output)


def _stitch_with_xfade(
    clips: Sequence[Path],
    beats: Sequence[Beat],
    render: RenderSettings,
    output: Path,
) -> Path:
    """Build one filter graph chaining xfade across every boundary.

    The offset arithmetic is the whole trick and it is easy to get wrong.
    ``xfade`` plays the first input up to ``offset``, blends for ``duration``,
    then continues with the second input, so::

        output_length = offset + length(second input)

    Two rules keep it from silently swallowing footage, both learned the hard
    way — when they are violated ffmpeg exits 0 and just truncates:

    1. **The blend must be at least two frames long.** A sub-frame ``duration``
       (0.04s at 24fps is less than one frame) leaves xfade with no frame pair
       to interpolate, and the chain collapses to the length of a single clip.
       Hard cuts therefore use ``2/fps``, not a hard-coded seconds value.

    2. **The blend must end before the first input's last frame.** ``offset``
       gets one extra frame of headroom for that.
    """
    inputs: list[str] = []
    for clip in clips:
        inputs += ["-i", str(clip)]

    frame = 1.0 / max(1, render.fps)
    min_blend = 2 * frame          # shortest blend that still reads as a cut
    headroom = frame

    durations = [b.duration_s for b in beats]
    filters: list[str] = []
    current_label = "0:v"
    elapsed = durations[0]

    for i in range(1, len(clips)):
        name, raw_duration = TRANSITIONS.get(beats[i].transition_in, ("fade", 0.25))
        if not name:
            name, raw_duration = "fade", min_blend

        # A transition may never eat more than a third of either neighbour, nor
        # more than the accumulated stream it has to fit inside.
        max_duration = min(elapsed, durations[i - 1], durations[i]) / 3.0
        duration = max(min_blend, min(raw_duration, max_duration))
        offset = max(0.0, elapsed - duration - headroom)

        out_label = f"x{i}"
        filters.append(
            f"[{current_label}][{i}:v]xfade=transition={name}:duration={duration:.4f}:"
            f"offset={offset:.4f}[{out_label}]"
        )
        elapsed = offset + durations[i]
        current_label = out_label

    filters.append(f"[{current_label}]fps={render.fps},format={render.pixel_format}[v]")

    ffmpeg.run(
        [
            *inputs,
            "-filter_complex", ";".join(filters),
            "-map", "[v]",
            "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
            "-pix_fmt", render.pixel_format,
            str(output),
        ],
        label=f"xfade stitch ({len(clips)} clips)",
        timeout=1800,
    )
    return output


# --------------------------------------------------------------------------- #
# Step 3 — loop
# --------------------------------------------------------------------------- #
def close_loop(
    video: Path,
    concept: Concept,
    render: RenderSettings,
    output: Path,
    work_dir: Path,
) -> Path:
    """Make the end flow back into the beginning.

    Rewatches are the cheapest views on the platform: a viewer who doesn't
    notice the restart watches twice, and the second view costs nothing.

    * ``crossfade_to_start`` — dissolve the tail into a copy of the opening
      frames. Works on almost any footage and is nearly invisible at speed.
    * ``freeze_hold``       — hold the final frame briefly. Good when the payoff
      is a pose worth reading.
    * ``hard_cut``          — leave it alone. Right when the payoff *is* the end.
    """
    if concept.loop_strategy == "hard_cut":
        return video
    if _is_fresh(output, [video]):
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(video)

    if concept.loop_strategy == "freeze_hold":
        hold = 0.35
        ffmpeg.run(
            [
                "-i", str(video),
                "-vf", f"tpad=stop_mode=clone:stop_duration={hold}",
                "-r", str(render.fps),
                "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
                "-pix_fmt", render.pixel_format,
                str(output),
            ],
            label="loop freeze_hold",
        )
        return output

    # crossfade_to_start
    frame = 1.0 / max(1, render.fps)
    blend = min(0.5, max(0.25, info.duration_s * 0.05))
    head = work_dir / "loop_head.mp4"
    ffmpeg.run(
        [
            "-i", str(video), "-t", f"{blend:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(render.crf),
            "-pix_fmt", render.pixel_format, str(head),
        ],
        label="loop head extract",
    )
    # Same one-frame headroom rule as the stitch graph — a blend that ends on
    # the final frame makes xfade drop everything after it.
    offset = max(0.0, info.duration_s - blend - frame)
    ffmpeg.run(
        [
            "-i", str(video), "-i", str(head),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={blend:.4f}:offset={offset:.4f},"
            f"fps={render.fps},format={render.pixel_format}[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
            "-pix_fmt", render.pixel_format,
            str(output),
        ],
        label="loop crossfade_to_start",
    )
    return output


# --------------------------------------------------------------------------- #
# Step 4 — final mux
# --------------------------------------------------------------------------- #
def mux(
    video: Path,
    audio: Path | None,
    settings: Settings,
    output: Path,
    *,
    subtitle_file: Path | None = None,
    subtitle_style: str = "",
) -> Path:
    """Encode the deliverable: video + music + burned subtitles.

    Audio is loudness-normalised to the platform target so a viewer scrolling
    from someone else's video doesn't get blasted or left straining. Subtitles
    are burned rather than sidecarred because TikTok ignores sidecar files.
    """
    render = settings.render
    output.parent.mkdir(parents=True, exist_ok=True)

    video_filters: list[str] = []
    if subtitle_file and Path(subtitle_file).exists():
        escaped = ffmpeg.escape_path_for_filter(subtitle_file)
        style = f":force_style='{subtitle_style}'" if subtitle_style else ""
        video_filters.append(f"subtitles='{escaped}'{style}")
    video_filters.append(f"format={render.pixel_format}")

    args: list[str] = ["-i", str(video)]
    if audio and Path(audio).exists():
        args += ["-i", str(audio)]

    args += ["-vf", ",".join(video_filters)]

    if audio and Path(audio).exists():
        args += [
            "-af",
            f"loudnorm=I={render.loudness_lufs}:TP=-1.5:LRA=11,"
            f"afade=t=in:st=0:d=0.25,"
            f"aresample={render.audio_sample_rate}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:a", render.audio_codec, "-b:a", render.audio_bitrate,
            "-ar", str(render.audio_sample_rate), "-ac", "2",
            "-shortest",
        ]
    else:
        args += ["-an"]

    args += [
        "-c:v", render.video_codec,
        "-preset", render.preset,
        "-crf", str(render.crf),
        "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", render.pixel_format,
        "-r", str(render.fps),
        # Keyframe every second: better seeking and better first-frame quality
        # for the platform's own thumbnail extraction.
        "-g", str(render.fps),
        "-movflags", "+faststart",
        str(output),
    ]

    ffmpeg.run(args, label=f"mux {output.name}", timeout=1800)
    return output


def extract_cover(video: Path, output: Path, at_s: float = 0.4) -> Path:
    """Grab a cover still. 0.4s in — past any transition, still on the hook."""
    return ffmpeg.extract_frame(video, output, at_s)
