"""Loop-beat derivation — the final beat, for free.

Every video ends on a short ``loop`` beat whose whole job is to rhyme with
frame one so the restart is invisible. Generating that beat separately is the
worst trade in the pipeline:

* it is a **separately billed request** — and because models bill in discrete
  blocks (5s minimum is typical), a 1.6-second beat costs the same as a
  5-second one;
* it is generated *independently*, so the animal's markings, costume and
  framing drift away from beat one — exactly the thing the beat exists to
  match;
* and it is 13% of all generation requests.

Deriving it from the opening of beat one instead costs nothing, and the loop is
*better*: the last frames are literally the first frames, so the seam is exact
rather than approximate. The identity question disappears — it is the same
footage, so it is the same kitten.

The trim re-encodes rather than stream-copying. A stream copy starts at the
nearest keyframe before the cut, which would silently shift the loop by up to a
keyframe interval — the one thing this beat cannot tolerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import RenderSettings
from ..logging_setup import get_logger
from . import ffmpeg

log = get_logger("media.loop")

#: Tolerance when comparing the source clip's length against what we need.
#: One frame at 24fps, which is more than any rounding error we produce.
DURATION_EPSILON_S = 0.042


@dataclass(frozen=True)
class DerivationCheck:
    """Whether a loop beat can be derived, and why not when it can't."""

    ok: bool
    reason: str = ""
    source_duration_s: float = 0.0

    def __bool__(self) -> bool:
        return self.ok


def can_derive(source: Path | str | None, loop_duration_s: float) -> DerivationCheck:
    """Can ``loop_duration_s`` be taken from the opening of ``source``?

    Checks the file exists, is non-empty, is actually decodable, and is long
    enough. Each failure returns a reason rather than raising, because the
    caller's job on failure is to fall back to generating the beat — a video
    that costs one extra request beats a video that doesn't exist.
    """
    if source is None:
        return DerivationCheck(False, "no source clip was recorded for the first beat")

    path = Path(source)
    if not path.exists():
        return DerivationCheck(False, f"source clip is missing: {path.name}")
    if path.stat().st_size == 0:
        return DerivationCheck(False, f"source clip is empty: {path.name}")
    if loop_duration_s <= 0:
        return DerivationCheck(False, f"loop duration {loop_duration_s}s is not positive")

    try:
        info = ffmpeg.probe(path)
    except Exception as exc:  # unreadable file — treat as absent, don't crash
        return DerivationCheck(False, f"source clip is unreadable: {exc}")

    if info.duration_s <= 0:
        return DerivationCheck(
            False, f"source clip reports no duration", source_duration_s=info.duration_s
        )

    if info.duration_s + DURATION_EPSILON_S < loop_duration_s:
        return DerivationCheck(
            False,
            f"source clip is {info.duration_s:.2f}s but the loop beat needs "
            f"{loop_duration_s:.2f}s",
            source_duration_s=info.duration_s,
        )

    return DerivationCheck(True, source_duration_s=info.duration_s)


def derive_loop_clip(
    source: Path,
    loop_duration_s: float,
    output: Path,
    render: RenderSettings,
) -> Path:
    """Write the opening ``loop_duration_s`` of ``source`` to ``output``.

    Deterministic: the same source and duration always produce the same bytes,
    which is what lets a resumed build reuse the file and what the tests assert.
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    # `-ss 0` before `-i` plus a re-encode guarantees the output starts on a
    # fresh keyframe at exactly t=0.
    ffmpeg.run(
        [
            "-ss", "0",
            "-i", str(source),
            "-t", f"{loop_duration_s:.3f}",
            "-an",
            "-vf", f"fps={render.fps},format={render.pixel_format}",
            "-c:v", "libx264",
            "-preset", render.preset,
            "-crf", str(render.crf),
            "-pix_fmt", render.pixel_format,
            str(output),
        ],
        label=f"derive loop clip {output.name}",
        timeout=300,
    )
    return output
