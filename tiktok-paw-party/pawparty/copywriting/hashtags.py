"""Hashtag construction.

A hashtag set is built in tiers rather than picked at random, because the tiers
do different jobs:

* **core** — 2-3 tags that tell the algorithm what the account *is*. Constant.
  Consistency here is what builds a stable audience profile over months.
* **content** — derived from this specific video: species, dance, theme,
  setting, occasion. This is the part that varies video to video.
* **broad** — one or two high-volume discovery tags. Useful, but they are also
  where you compete with everyone, so they never dominate the set.
* **rotating** — an operator-maintained list in the channel config for trends
  you actually verified. Deliberately *not* auto-fetched: TikTok has no public
  trending-hashtag API, and scraping one would be a fragile, ToS-risky
  dependency in a pipeline that has to run unattended every day.

Total is capped (default 8). Long hashtag walls dilute relevance and read as
spam.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..config import ChannelProfile
from ..models import Concept
from ..util import pluralize, seeded_rng, slugify, strip_article

_INVALID = re.compile(r"[^a-z0-9]")


def normalize_tag(raw: str) -> str:
    """`"Disco Night!" -> "disconight"`. Returns '' for unusable input."""
    tag = _INVALID.sub("", str(raw).lower().lstrip("#"))
    return tag if 2 <= len(tag) <= 30 else ""


class HashtagBuilder:
    """Builds a deterministic, tiered hashtag set for a concept."""

    def __init__(self, channel: ChannelProfile) -> None:
        cfg = channel.hashtags or {}
        self.core: list[str] = [normalize_tag(t) for t in cfg.get("core", [])]
        self.broad: list[str] = [normalize_tag(t) for t in cfg.get("broad", [])]
        self.rotating: list[str] = [normalize_tag(t) for t in cfg.get("rotating", [])]
        self.occasion_map: dict[str, list[str]] = {
            k: [normalize_tag(t) for t in v] for k, v in (cfg.get("occasions") or {}).items()
        }
        self.max_total: int = int(cfg.get("max_total", 8))
        self.broad_count: int = int(cfg.get("broad_count", 2))
        self.rotating_count: int = int(cfg.get("rotating_count", 1))
        self.banned: set[str] = {normalize_tag(t) for t in cfg.get("banned", [])}
        #: tag -> the species it belongs to. A cat tag on a dogs-only video is
        #: worse than no tag: it pulls the wrong audience, who then scroll.
        self.tag_species: dict[str, str] = {
            normalize_tag(tag): species
            for species, tags in (cfg.get("species_tags") or {}).items()
            for tag in tags
        }

    # ------------------------------------------------------------------ #
    def build(self, concept: Concept, extra_ideas: Iterable[str] = ()) -> list[str]:
        rng = seeded_rng(concept.id, "hashtags")
        cast_species = {m.species for m in concept.cast}
        out: list[str] = []

        def add(tags: Iterable[str]) -> None:
            for tag in tags:
                tag = normalize_tag(tag)
                if not tag or tag in out or tag in self.banned:
                    continue
                species = self.tag_species.get(tag)
                if species and species not in cast_species:
                    continue
                out.append(tag)

        add(self.core)
        add(self._content_tags(concept))
        add(extra_ideas)

        if self.rotating:
            add(rng.sample(self.rotating, k=min(self.rotating_count, len(self.rotating))))
        if self.broad:
            add(rng.sample(self.broad, k=min(self.broad_count, len(self.broad))))

        return out[: self.max_total]

    def render(self, tags: Iterable[str]) -> str:
        """Space-separated `#tag` string, ready to append to a caption."""
        return " ".join(f"#{t}" for t in tags)

    # ------------------------------------------------------------------ #
    def _content_tags(self, concept: Concept) -> list[str]:
        tags: list[str] = []

        species = sorted({m.species for m in concept.cast})
        for name in species:
            tags.append(name)                # kitten
            tags.append(pluralize(name))     # kittens (not "kittens" + "kittenss")
        if len(species) > 1:
            tags.append("catsanddogs")

        # Articles have to go before slugifying, or you ship "#ahuladance".
        tags.append(slugify(strip_article(concept.dance_label)).replace("-", ""))
        tags.append(slugify(strip_article(concept.theme_label)).replace("-", ""))
        tags.append(slugify(strip_article(concept.setting_label)).replace("-", ""))

        if concept.occasion and concept.occasion != "none":
            tags.extend(self.occasion_map.get(concept.occasion, [concept.occasion.replace("_", "")]))

        # Drop the accidental essays that slugified labels can produce.
        return [t for t in (normalize_tag(t) for t in tags) if t and len(t) <= 22]
