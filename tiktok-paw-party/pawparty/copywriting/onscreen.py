"""On-screen text plan (the burned-in "subtitles").

These videos have no dialogue, so there is nothing to transcribe. What burns in
instead is a small number of **timed text cards** that do retention work:

    0.0s        the hook, on screen before the viewer can decide to scroll
    ~switch     a one-line nudge at the point where drop-off peaks
    last beat   a comment-bait line ("who won?") while the loop restarts

Text is placed inside the safe area — TikTok's own UI eats roughly the bottom
20% and the top 12% — and is deliberately sparse. Wall-to-wall karaoke captions
on a wordless dance video read as noise.
"""

from __future__ import annotations

from typing import Any

from ..models import Concept

#: Alignment codes for ASS (numpad layout): 2 = bottom-centre, 5 = top-centre,
#: 8 = top-centre for \an, we use 2/8 plus explicit margins for placement.
POSITION_TOP = 8
POSITION_CENTER = 5
POSITION_BOTTOM = 2


def build_subtitle_lines(
    concept: Concept,
    onscreen_hook: str,
    comment_seed: str = "",
    *,
    max_cards: int = 3,
    actual_duration_s: float | None = None,
) -> list[dict[str, Any]]:
    """Return timed text cards as ``{start, end, text, position, emphasis}``.

    Times are absolute seconds within the finished video, derived from the beat
    durations so cards always land on a cut rather than mid-shot.

    ``actual_duration_s`` is the measured length of the stitched video, which is
    a little shorter than the sum of the beats because transitions consume time
    from both sides. Passing it rescales the cards so the closing call-to-action
    doesn't run past the end of the file.
    """
    if not concept.beats:
        return []

    # Absolute start time of each beat.
    starts: list[float] = []
    clock = 0.0
    for beat in concept.beats:
        starts.append(clock)
        clock += beat.duration_s

    total = clock
    if actual_duration_s and clock > 0:
        scale = actual_duration_s / clock
        starts = [s * scale for s in starts]
        total = actual_duration_s

    cards: list[dict[str, Any]] = []

    # 1. The hook. On screen from frame 1; off before it overstays.
    if onscreen_hook:
        hook_end = min(2.6, max(1.6, concept.beats[0].duration_s * 0.85))
        cards.append(
            {
                "start": 0.0,
                "end": round(hook_end, 2),
                "text": onscreen_hook.upper(),
                "position": POSITION_TOP,
                "emphasis": "hook",
            }
        )

    # 2. The mid-video nudge, placed on the switch beat (or the middle beat).
    switch_index = next(
        (i for i, b in enumerate(concept.beats) if b.role == "switch"),
        len(concept.beats) // 2 if len(concept.beats) > 2 else -1,
    )
    if 0 < switch_index < len(starts) and len(concept.beats) > 2:
        beat = concept.beats[switch_index]
        text = _switch_line(concept)
        if text:
            cards.append(
                {
                    "start": round(starts[switch_index] + 0.15, 2),
                    "end": round(starts[switch_index] + min(2.4, beat.duration_s * 0.8), 2),
                    "text": text,
                    "position": POSITION_TOP,
                    "emphasis": "beat",
                }
            )

    # 3. The comment bait, riding the payoff into the loop.
    if comment_seed and total > 10:
        tail_start = max(0.0, total - min(3.2, total * 0.28))
        cards.append(
            {
                "start": round(tail_start, 2),
                "end": round(total - 0.15, 2),
                "text": _shorten(comment_seed, 34).upper(),
                "position": POSITION_TOP,
                "emphasis": "cta",
            }
        )

    return cards[:max_cards]


def _switch_line(concept: Concept) -> str:
    """A short line that re-sells the video at the drop-off point."""
    if len(concept.cast) > 1:
        return "NOW WATCH THIS ONE"
    if concept.occasion != "none":
        return concept.occasion.replace("_", " ").upper() + " MODE"
    return "WAIT FOR IT"


def _shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0]
