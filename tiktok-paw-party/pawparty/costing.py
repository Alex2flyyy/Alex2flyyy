"""Preflight cost estimation.

The budget guard in :mod:`pawparty.budget` is a *stop*: it refuses a call once
the ledger says you've spent enough. Useful, but it only ever tells you after
the fact, one call at a time — which is how you end up with a batch that halts
at video four with a bare "budget exceeded".

This module answers the question *before* anything is generated: given these
concepts and these providers, how many billable requests is that, how many
billed seconds, and what will it cost? Concepts are free to produce, so the
estimate is available at full accuracy before the first cent is spent.

Accuracy comes from two things the naive sum gets wrong:

* **Billing granularity.** Models bill in discrete blocks, so a 1.6-second beat
  costs the same as a 5-second one. Summing requested seconds understates a
  real bill by well over half. Each provider reports its own granularity via
  :meth:`~pawparty.providers.base.VideoProvider.billed_seconds`.
* **Derived beats.** A loop beat reused from beat one is not a request at all.
  Counting it inflates both the estimate and the budget check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import Settings
from .models import Concept


@dataclass
class CostEstimate:
    """What a batch will cost, broken down by stage."""

    videos: int = 0
    #: Beats that will be sent to the video provider.
    video_requests: int = 0
    video_billed_seconds: float = 0.0
    video_usd: float = 0.0

    image_requests: int = 0
    image_usd: float = 0.0

    music_requests: int = 0
    music_usd: float = 0.0

    llm_usd: float = 0.0

    #: Beats derived locally instead of generated.
    derived_beats: int = 0
    #: What those beats would have cost had they been generated.
    avoided_requests: int = 0
    avoided_billed_seconds: float = 0.0
    avoided_usd: float = 0.0

    providers: dict[str, str] = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        return round(self.video_usd + self.image_usd + self.music_usd + self.llm_usd, 4)

    @property
    def per_video_usd(self) -> float:
        return round(self.total_usd / self.videos, 4) if self.videos else 0.0

    @property
    def is_free(self) -> bool:
        return self.total_usd <= 0.0

    @property
    def saving_pct(self) -> float:
        """Percentage saved against generating every beat."""
        baseline = self.video_usd + self.avoided_usd
        if baseline <= 0:
            return 0.0
        return round(self.avoided_usd / baseline * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "videos": self.videos,
            "video_requests": self.video_requests,
            "video_billed_seconds": round(self.video_billed_seconds, 2),
            "video_usd": round(self.video_usd, 4),
            "image_requests": self.image_requests,
            "image_usd": round(self.image_usd, 4),
            "music_requests": self.music_requests,
            "music_usd": round(self.music_usd, 4),
            "llm_usd": round(self.llm_usd, 4),
            "derived_beats": self.derived_beats,
            "avoided_requests": self.avoided_requests,
            "avoided_billed_seconds": round(self.avoided_billed_seconds, 2),
            "avoided_usd": round(self.avoided_usd, 4),
            "saving_pct": self.saving_pct,
            "total_usd": self.total_usd,
            "per_video_usd": self.per_video_usd,
            "providers": self.providers,
        }

    def summary(self) -> str:
        if self.is_free:
            return (
                f"{self.videos} video(s), {self.video_requests} generation request(s) — "
                f"$0.00 (free providers)"
            )
        text = (
            f"{self.videos} video(s): {self.video_requests} clip request(s), "
            f"{self.video_billed_seconds:.0f} billed second(s) — "
            f"${self.total_usd:.2f} (${self.per_video_usd:.2f}/video)"
        )
        if self.derived_beats:
            text += (
                f"; {self.derived_beats} loop beat(s) derived locally, "
                f"saving ${self.avoided_usd:.2f} ({self.saving_pct:.0f}%)"
            )
        return text

    def report_lines(self) -> list[str]:
        """Multi-line breakdown for the CLI."""
        lines = [
            f"  videos                {self.videos}",
            f"  clip requests         {self.video_requests}",
            f"  billed clip seconds   {self.video_billed_seconds:.0f}",
            f"  clip cost             ${self.video_usd:.2f}",
        ]
        if self.image_requests:
            lines.append(f"  still requests        {self.image_requests}  (${self.image_usd:.2f})")
        if self.music_usd:
            lines.append(f"  music                 ${self.music_usd:.2f}")
        if self.llm_usd:
            lines.append(f"  copywriting           ${self.llm_usd:.2f}")
        if self.derived_beats:
            lines.append(
                f"  loop beats derived    {self.derived_beats} "
                f"(saves {self.avoided_requests} request(s), "
                f"{self.avoided_billed_seconds:.0f}s, ${self.avoided_usd:.2f})"
            )
        lines.append(f"  TOTAL                 ${self.total_usd:.2f}"
                     f"  (${self.per_video_usd:.2f}/video)")
        return lines


def estimate_batch(
    concepts: Sequence[Concept],
    providers: Any,
    settings: Settings,
) -> CostEstimate:
    """Cost a batch of concepts without generating anything.

    ``providers`` is a :class:`~pawparty.providers.registry.ProviderBundle`.
    Beats carrying ``derived_from`` are counted as avoided rather than billed.
    """
    estimate = CostEstimate(
        videos=len(concepts),
        providers=providers.describe() if hasattr(providers, "describe") else {},
    )

    video = providers.video
    image = providers.image
    music = providers.music
    llm = providers.llm

    uses_stills = _uses_first_frames(providers)

    for concept in concepts:
        for beat in concept.beats:
            billed = float(video.billed_seconds(beat.duration_s))
            cost = float(video.estimate_cost(beat.duration_s))

            if beat.derived_from:
                estimate.derived_beats += 1
                estimate.avoided_requests += 1
                estimate.avoided_billed_seconds += billed
                estimate.avoided_usd += cost
                continue

            estimate.video_requests += 1
            estimate.video_billed_seconds += billed
            estimate.video_usd += cost

            if uses_stills:
                estimate.image_requests += 1
                estimate.image_usd += float(image.estimate_cost())

        estimate.music_requests += 1
        estimate.music_usd += float(music.estimate_cost(concept.duration_s + 0.6))

        if getattr(llm, "name", "offline") != "offline":
            estimate.llm_usd += float(llm.estimate_cost())

    estimate.video_usd = round(estimate.video_usd, 4)
    estimate.image_usd = round(estimate.image_usd, 4)
    estimate.music_usd = round(estimate.music_usd, 4)
    estimate.llm_usd = round(estimate.llm_usd, 4)
    estimate.avoided_usd = round(estimate.avoided_usd, 4)
    return estimate


def _uses_first_frames(providers: Any) -> bool:
    """Mirror of the pipeline's stills decision, so the estimate matches."""
    video = providers.video
    input_map = getattr(video, "input_map", None)
    if isinstance(input_map, dict) and input_map.get("first_frame"):
        return True
    return (
        getattr(video, "name", "") == "synth"
        and getattr(providers.image, "name", "") == "synth"
    )
