"""Beat planning — the retention skeleton behind every video.

Length is not chosen at random; it is chosen from *duration bands* that each
imply a different shot structure. Short videos win on loop rate, long ones win
on watch time, and mixing them keeps the account's average-view-duration signal
healthy across formats.

Every plan follows the same retention spine:

    HOOK      0.0 - 2.0s   The promise. Motion already in progress on frame 1,
                           subject centred, on-screen text asks or claims
                           something. No build-up, no logo, no fade-in.
    BUILD     the middle   Escalation. Each beat must add a new visual fact
                           (new costume, new angle, new dancer) or it gets cut.
    SWITCH    ~60-70%      A pattern interrupt: tempo change, new character,
                           camera whip. This is where mid-video drop-off lives.
    PAYOFF    last ~25%    The reward the hook promised — the funniest beat.
    LOOP      final 0.6s   Visually rhymes with frame 1 so the restart is
                           seamless and the viewer watches twice without
                           deciding to.

Beat durations are jittered per concept so cuts never land on the same rhythm
twice, which is what makes a feed of 7 daily videos feel authored rather than
stamped out.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

from ..models import Beat
from ..util import jitter


@dataclass(frozen=True)
class DurationBand:
    """A length archetype with its own pacing rules."""

    id: str
    label: str
    min_s: float
    max_s: float
    beat_count: tuple[int, int]     # inclusive range
    weight: float
    notes: str = ""


#: Default ladder. Overridable per channel via `structure.duration_bands`.
DEFAULT_BANDS: tuple[DurationBand, ...] = (
    DurationBand("snap", "Snap loop", 9.0, 13.0, (2, 3), 0.28,
                 "Maximum loop rate. One idea, one punchline, repeat."),
    DurationBand("short", "Short set", 14.0, 21.0, (3, 4), 0.32,
                 "Hook + two escalations + payoff. The workhorse format."),
    DurationBand("standard", "Standard", 22.0, 32.0, (4, 6), 0.26,
                 "Room for a real switch beat and a costume change."),
    DurationBand("extended", "Extended", 33.0, 45.0, (6, 8), 0.14,
                 "Mini-narrative. Only worth it when the concept has stages."),
)

#: Role assignment per beat count. Index 0 is always the hook, last is the loop.
_ROLE_SPINE = ["hook", "build", "switch", "build", "payoff", "loop"]


def load_bands(structure: dict[str, Any]) -> list[DurationBand]:
    """Read duration bands from a channel's `structure` block, or use defaults."""
    raw = structure.get("duration_bands")
    if not raw:
        return list(DEFAULT_BANDS)
    bands = []
    for entry in raw:
        bands.append(
            DurationBand(
                id=entry["id"],
                label=entry.get("label", entry["id"].title()),
                min_s=float(entry["min_s"]),
                max_s=float(entry["max_s"]),
                beat_count=(int(entry["beat_count"][0]), int(entry["beat_count"][1])),
                weight=float(entry.get("weight", 1.0)),
                notes=entry.get("notes", ""),
            )
        )
    return bands


def choose_band(rng: random.Random, bands: Sequence[DurationBand]) -> DurationBand:
    from ..util import weighted_choice

    return weighted_choice(rng, list(bands), [b.weight for b in bands])


def assign_roles(beat_count: int) -> list[str]:
    """Map a beat count onto the retention spine.

    Small counts collapse the middle; large counts repeat `build` so escalation
    keeps going, but there is always exactly one hook, one payoff and one loop
    beat (a 2-beat video merges payoff into the loop).
    """
    if beat_count <= 2:
        return ["hook", "payoff_loop"]
    if beat_count == 3:
        return ["hook", "build", "payoff_loop"]

    roles = ["hook"]
    middle = beat_count - 3  # minus hook, payoff, loop
    switch_at = max(1, round(middle * 0.6))
    for i in range(middle):
        roles.append("switch" if i == switch_at - 1 else "build")
    roles.append("payoff")
    roles.append("loop")
    return roles


#: Per-role duration limits, in seconds: role -> (floor, ceiling).
#:
#: The ceiling on ordinary beats is not an aesthetic choice — most text-to-video
#: models top out around 5-10 seconds per generation, so a beat longer than that
#: either can't be made or has to be retimed. Keeping beats at 4-7s means one
#: beat is one generation.
ROLE_LIMITS: dict[str, tuple[float, float]] = {
    "hook": (1.8, 4.0),          # the hook must land inside 2s; the shot can
                                 # run a little longer, but it never drags
    "build": (1.5, 8.0),
    "switch": (1.5, 8.0),
    "payoff": (1.8, 8.0),
    "payoff_loop": (2.5, 9.0),
    "loop": (0.7, 1.6),          # just enough to sell the seam
}

#: Target seconds per beat, used to derive beat count from total length.
TARGET_BEAT_S = 5.0


def beat_count_for(total_s: float, band: DurationBand) -> int:
    """How many beats a video of ``total_s`` should have.

    Sampling the count independently of the length produces 11-second shots that
    no model can generate. Deriving it keeps every beat near
    :data:`TARGET_BEAT_S` while staying inside the band's declared range.
    """
    ideal = round(total_s / TARGET_BEAT_S)
    return max(band.beat_count[0], min(band.beat_count[1], int(ideal)))


def plan_durations(
    rng: random.Random,
    total_s: float,
    roles: Sequence[str],
) -> list[float]:
    """Split ``total_s`` across beats using role-aware weights + jitter.

    Weights set the shape; :data:`ROLE_LIMITS` sets the bounds. Because clamping
    one beat frees (or steals) time from the others, the two are reconciled
    iteratively rather than in a single pass — a single pass is what lets a
    "capped" beat quietly end up over its cap after redistribution.
    """
    base_weights = {
        "hook": 0.75,
        "build": 1.15,
        "switch": 1.05,
        "payoff": 1.25,
        "payoff_loop": 1.4,
        "loop": 0.4,
    }
    weights = [max(0.2, jitter(rng, base_weights.get(role, 1.0), 0.12)) for role in roles]
    limits = [ROLE_LIMITS.get(role, (1.5, 8.0)) for role in roles]

    total_weight = sum(weights)
    durations = [total_s * w / total_weight for w in weights]

    # Reconcile: pin any beat that violates its limits, then redistribute the
    # remaining time across the beats that are still free to move.
    pinned: set[int] = set()
    for _ in range(len(roles) + 2):
        changed = False
        for i, (value, (floor, ceiling)) in enumerate(zip(durations, limits)):
            if i in pinned:
                continue
            if value < floor:
                durations[i], changed = floor, True
                pinned.add(i)
            elif value > ceiling:
                durations[i], changed = ceiling, True
                pinned.add(i)

        free = [i for i in range(len(roles)) if i not in pinned]
        remaining = total_s - sum(durations[i] for i in pinned)
        if not free:
            break
        free_weight = sum(weights[i] for i in free)
        for i in free:
            durations[i] = remaining * weights[i] / free_weight
        if not changed:
            break

    return [round(value, 2) for value in durations]


def build_beats(
    rng: random.Random,
    *,
    total_s: float,
    beat_count: int,
    actions: dict[str, str],
    cameras: Sequence[tuple[str, str]],
    transitions: Sequence[str],
    loop_strategy: str,
) -> list[Beat]:
    """Assemble the concrete beat list.

    ``actions`` maps role -> action text (supplied by the concept generator so
    the wording matches the chosen dance style and cast).
    ``cameras`` is a list of ``(camera_id, camera_prompt)`` pairs, one per beat.
    """
    roles = assign_roles(beat_count)
    durations = plan_durations(rng, total_s, roles)

    beats: list[Beat] = []
    for index, (role, duration) in enumerate(zip(roles, durations), start=1):
        camera_id, camera_prompt = cameras[(index - 1) % len(cameras)]
        if index == 1:
            transition = "cut"  # never open on a transition; frame 1 must land
        else:
            transition = transitions[(index - 2) % len(transitions)]
        beats.append(
            Beat(
                index=index,
                role=role,
                duration_s=duration,
                action=actions.get(role, actions.get("build", "dancing joyfully")),
                camera=camera_id,
                camera_prompt=camera_prompt,
                transition_in=transition,
            )
        )

    # The loop beat must visually rhyme with beat 1 for a seamless restart.
    if loop_strategy != "hard_cut" and len(beats) > 1 and beats[-1].role == "loop":
        beats[-1].camera = beats[0].camera
        beats[-1].camera_prompt = beats[0].camera_prompt
        beats[-1].transition_in = "crossfade"
    return beats


def total_from_band(rng: random.Random, band: DurationBand) -> float:
    """A jittered length inside the band, rounded to the nearest 0.5s."""
    raw = rng.uniform(band.min_s, band.max_s)
    return round(raw * 2) / 2
