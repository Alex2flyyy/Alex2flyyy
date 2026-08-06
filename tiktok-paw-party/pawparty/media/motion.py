"""Camera motion.

Two distinct jobs share this module:

1. **Normalisation** — whatever the provider returns (square, 16:9, 24fps,
   30fps, 4 seconds when we asked for 3.5) becomes exactly 1080x1920 at the
   project frame rate and exactly the beat's duration. Everything downstream
   assumes this has happened.

2. **Added camera motion** — a subtle continuous push, pull, drift or handheld
   float layered on top of the generated footage. This is the single cheapest
   upgrade to perceived production value: generated clips often have a static
   or floaty camera, and a deliberate move makes a shot read as *directed*.

The moves are expressed as ffmpeg filter chains built by
:func:`motion_filter`, which returns a chain fragment (no input/output labels)
so callers can splice it into a larger ``-vf`` or ``-filter_complex``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..config import RenderSettings


@dataclass(frozen=True)
class MotionSpec:
    """One camera move: id, human label, and the filter builder."""

    id: str
    label: str
    build: Callable[[int, int, int, float, float], str]
    #: How much oversampling the move needs (1.0 = none). Panning/zooming crops
    #: into a larger frame, so we scale up first to avoid softness.
    headroom: float = 1.0


def _push_in(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    """Slow dolly toward the subject. The default for hook beats."""
    zoom = 1.0 + 0.14 * intensity
    frames = max(2, int(duration * fps))
    return (
        f"scale={int(w * 1.35)}:{int(h * 1.35)},"
        f"zoompan=z='1+({zoom - 1:.4f})*on/{frames}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={w}x{h}:fps={fps}"
    )


def _pull_out(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    """Reveal. Starts tight, widens — good for a payoff beat."""
    zoom = 1.0 + 0.16 * intensity
    frames = max(2, int(duration * fps))
    return (
        f"scale={int(w * 1.35)}:{int(h * 1.35)},"
        f"zoompan=z='{zoom:.4f}-({zoom - 1:.4f})*on/{frames}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={w}x{h}:fps={fps}"
    )


def _pan_lr(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    over = 1.0 + 0.18 * intensity
    return (
        f"scale={int(w * over)}:{int(h * over)},"
        f"crop={w}:{h}:'(iw-{w})*t/{max(duration, 0.1):.3f}':'(ih-{h})/2'"
    )


def _pan_rl(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    over = 1.0 + 0.18 * intensity
    return (
        f"scale={int(w * over)}:{int(h * over)},"
        f"crop={w}:{h}:'(iw-{w})*(1-t/{max(duration, 0.1):.3f})':'(ih-{h})/2'"
    )


def _tilt_up(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    over = 1.0 + 0.20 * intensity
    return (
        f"scale={int(w * over)}:{int(h * over)},"
        f"crop={w}:{h}:'(iw-{w})/2':'(ih-{h})*(1-t/{max(duration, 0.1):.3f})'"
    )


def _handheld(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    """Organic float. Two out-of-phase sines so it never looks like a loop."""
    amp = 12 * intensity
    over = 1.06
    return (
        f"scale={int(w * over)}:{int(h * over)},"
        f"crop={w}:{h}:"
        f"'(iw-{w})/2+{amp:.2f}*sin(t*1.7)':"
        f"'(ih-{h})/2+{amp * 0.8:.2f}*sin(t*2.3+1.1)'"
    )


def _orbit(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    """Fake orbit: circular crop path over an upscaled frame."""
    amp = 26 * intensity
    over = 1.14
    return (
        f"scale={int(w * over)}:{int(h * over)},"
        f"crop={w}:{h}:"
        f"'(iw-{w})/2+{amp:.2f}*sin(t*1.1)':"
        f"'(ih-{h})/2+{amp * 0.45:.2f}*cos(t*1.1)'"
    )


def _punch_in(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    """Stepped zoom that snaps every half second — reads as an edit, not a move.

    ``on`` is zoompan's output-frame counter, so ``on/(fps*0.5)`` advances one
    step per half second and ``mod(...,4)`` cycles it back to wide.
    """
    step = 0.07 * intensity
    period = max(1, int(fps * 0.5))
    return (
        f"scale={int(w * 1.3)}:{int(h * 1.3)},"
        f"zoompan=z='1+{step:.4f}*mod(floor(on/{period}),4)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={w}x{h}:fps={fps}"
    )


def _static(w: int, h: int, fps: int, duration: float, intensity: float) -> str:
    return f"scale={w}:{h}"


#: All available moves. Ids here must match `camera_moves` in the channel YAML.
MOTIONS: dict[str, MotionSpec] = {
    "slow_push_in": MotionSpec("slow_push_in", "slow push in", _push_in, 1.35),
    "slow_pull_out": MotionSpec("slow_pull_out", "slow pull out", _pull_out, 1.35),
    "pan_left_right": MotionSpec("pan_left_right", "pan left to right", _pan_lr, 1.18),
    "pan_right_left": MotionSpec("pan_right_left", "pan right to left", _pan_rl, 1.18),
    "tilt_up": MotionSpec("tilt_up", "tilt up", _tilt_up, 1.20),
    "handheld_float": MotionSpec("handheld_float", "handheld float", _handheld, 1.06),
    "orbit": MotionSpec("orbit", "orbit around subject", _orbit, 1.14),
    "punch_in": MotionSpec("punch_in", "rhythmic punch-in", _punch_in, 1.30),
    "static": MotionSpec("static", "locked off", _static, 1.0),
}

DEFAULT_MOTION = "handheld_float"


def motion_filter(
    move_id: str,
    render: RenderSettings,
    duration_s: float,
    *,
    intensity: float = 1.0,
) -> str:
    """Filter-chain fragment for ``move_id``. Unknown ids degrade to a default."""
    spec = MOTIONS.get(move_id) or MOTIONS[DEFAULT_MOTION]
    return spec.build(render.width, render.height, render.fps, duration_s, intensity)


def normalize_filter(render: RenderSettings) -> str:
    """Fit any input into the vertical frame without letterboxing.

    Scale to *cover* then centre-crop. A blurred-background pillarbox is the
    alternative, but for a subject-centred cute-animal clip, cropping keeps the
    face large — which is what the format rewards.
    """
    w, h = render.width, render.height
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},"
        f"setsar=1"
    )


def build_beat_chain(
    render: RenderSettings,
    move_id: str,
    duration_s: float,
    *,
    intensity: float = 1.0,
    add_grade: bool = True,
) -> str:
    """Full per-beat chain: normalise -> camera move -> grade -> fps/format.

    The grade is a light saturation and contrast lift. Phone screens in daylight
    eat contrast, and the whole aesthetic here is "bright and candy-coloured".
    """
    parts = [normalize_filter(render), motion_filter(move_id, render, duration_s, intensity=intensity)]
    if add_grade:
        parts.append("eq=saturation=1.14:contrast=1.06:brightness=0.012")
        parts.append("unsharp=5:5:0.45:5:5:0.0")
    parts.append(f"fps={render.fps}")
    parts.append(f"format={render.pixel_format}")
    return ",".join(parts)


def list_motions() -> list[tuple[str, str]]:
    return [(spec.id, spec.label) for spec in MOTIONS.values()]
