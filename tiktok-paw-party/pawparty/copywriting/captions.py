"""Caption generation and ranking.

Two sources, one interface:

* **Offline** (default): deterministic templates driven by the channel's
  ``copy.caption_patterns``. Zero cost, zero latency, never rate-limited, and
  reproducible from the concept seed.
* **LLM**: an :class:`~pawparty.providers.base.LLMProvider` writes fresh copy.
  Better variety; costs money; falls back to offline on any failure.

Either way the candidates go through the same ranking function, because the
thing that actually moves comments is structural — a question, a comparison, a
rating prompt — not cleverness.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..config import ChannelProfile
from ..logging_setup import get_logger
from ..models import Concept
from ..util import pluralize, seeded_rng, strip_article, truncate

log = get_logger("copy.captions")

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF❤️]"
)

#: Structures that reliably drive replies. Used both to generate and to score.
_ENGAGEMENT_MARKERS = {
    "question": (re.compile(r"\?\s*$|\?\s"), 3.0),
    "rating": (re.compile(r"\b(rate|1[-–]10|out of 10|score)\b", re.I), 2.5),
    # Anchored deliberately: an unanchored /who/ matches "someone who needs it",
    # which is not a comparison and scored ordinary statements to the top.
    "comparison": (re.compile(r"^(which|who|team)\b|\bvs\.?\b|\bor nah\b", re.I), 2.0),
    "imperative": (re.compile(r"^(tell|name|pick|guess|try|wait|watch|stop)\b", re.I), 1.5),
    "superlative": (re.compile(r"\b(best|cutest|funniest|hardest|impossible)\b", re.I), 1.2),
}


@dataclass
class CaptionCandidate:
    text: str
    score: float
    reasons: list[str]


class CaptionWriter:
    """Produces a ranked list of caption options for a concept."""

    def __init__(self, channel: ChannelProfile, llm: Any | None = None) -> None:
        self.channel = channel
        self.llm = llm
        self.cfg = channel.copy or {}
        self.max_chars = int(self.cfg.get("max_caption_chars", 90))
        self.count = int(self.cfg.get("caption_count", 5))
        self.max_emoji = int(self.cfg.get("max_emoji", 2))
        self.banned = [w.lower() for w in (channel.safety or {}).get("banned_words", [])]

    # ------------------------------------------------------------------ #
    @staticmethod
    def shape(caption: str) -> str:
        """A caption's structural fingerprint: its opening words, normalised.

        "Who did it better? 🐾" and "Who did it better? 😻" are the same caption
        as far as a viewer scrolling your profile is concerned.
        """
        words = re.sub(r"[^a-z0-9 ]", "", caption.lower()).split()
        # Two words, not three: "Rate this floss" and "Rate this waltz" differ
        # at word three but are the same caption to a human.
        return " ".join(words[:2])

    def write(
        self,
        concept: Concept,
        llm_prompt: str | None = None,
        *,
        avoid_shapes: Iterable[str] = (),
        avoid_hooks: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Return ``{captions, chosen_caption, onscreen_hook, comment_seed, alt_text,
        hashtag_ideas}``.

        Offline generation always runs, so LLM output is a *superset* — we rank
        both together and never end up with nothing.

        ``avoid_shapes`` carries the openings already used elsewhere in this
        batch. Ranking alone is deterministic, so without it the highest-scoring
        pattern wins every single video and the day's captions read as one
        template repeated seven times.
        """
        rng = seeded_rng(concept.id, "captions")
        offline = self._offline_candidates(concept, rng)
        extras: dict[str, Any] = {}

        if self.llm is not None and llm_prompt:
            try:
                payload = self.llm.write_copy(llm_prompt)
                llm_captions = [str(c) for c in payload.get("captions", []) if str(c).strip()]
                offline = llm_captions + offline
                extras = {
                    "onscreen_hook": payload.get("onscreen_hook", ""),
                    "comment_seed": payload.get("comment_seed", ""),
                    "alt_text": payload.get("alt_text", ""),
                    "hashtag_ideas": [str(h) for h in payload.get("hashtag_ideas", [])],
                }
            except Exception as exc:  # provider failures must never block a post
                log.warning(
                    "LLM copywriter failed (%s); using offline captions", exc,
                    extra={"concept": concept.id},
                )

        ranked = self.rank(offline, concept, avoid_shapes=avoid_shapes)
        captions = [c.text for c in ranked][: max(self.count, 3)]

        return {
            "captions": captions,
            "chosen_caption": captions[0] if captions else self._fallback(concept),
            "onscreen_hook": self._clean_hook(
                extras.get("onscreen_hook") or self._offline_hook(concept, rng, avoid_hooks)
            ),
            "comment_seed": extras.get("comment_seed") or self._offline_comment_seed(concept, rng),
            "alt_text": extras.get("alt_text") or self._alt_text(concept),
            "hashtag_ideas": extras.get("hashtag_ideas", []),
        }

    # ------------------------------------------------------------------ #
    # Offline generation
    # ------------------------------------------------------------------ #
    def _offline_candidates(self, concept: Concept, rng: random.Random) -> list[str]:
        patterns: list[str] = list(self.cfg.get("caption_patterns", []))
        if not patterns:
            patterns = [
                "Rate this {dance} from 1-10 {emoji}",
                "Which one wins? {emoji}",
                "Too cute to scroll past {emoji}",
                "Wait for the {payoff_hint} {emoji}",
                "{theme} but make it adorable {emoji}",
            ]
        # Sample more patterns than we need so ranking has real choice.
        picks = rng.sample(patterns, k=min(len(patterns), self.count + 3))
        out = []
        for pattern in picks:
            # Fields are rebuilt per pattern so each caption draws its own
            # emoji — five options ending in the identical pair reads as a
            # template, which is exactly what we're trying not to look like.
            try:
                text = pattern.format(**self._fields(concept, rng))
            except KeyError as exc:
                log.warning(
                    "caption pattern references unknown field %s; skipping", exc,
                    extra={"concept": concept.id},
                )
                continue
            out.append(self._tidy(text))
        return out

    def _fields(self, concept: Concept, rng: random.Random) -> dict[str, str]:
        emoji_pool = list(self.cfg.get("emoji", ["🐱", "🐶", "✨", "🥹", "😭", "🕺"]))
        species = sorted({m.species for m in concept.cast})
        emoji_count = rng.randint(1, max(1, self.max_emoji))
        return {
            "dance": strip_article(concept.dance_label.lower()),
            "theme": concept.theme_label,
            "setting": concept.setting_label.lower(),
            "animal": species[0] if species else "pet",
            "animals": " and ".join(pluralize(s) for s in species) if species else "pets",
            "lead": strip_article(concept.cast[0].label.lower()) if concept.cast else "this one",
            "music": concept.music.label.lower(),
            "payoff_hint": concept.payoff_hint or "ending",
            "occasion": concept.occasion.replace("_", " "),
            "emoji": "".join(rng.sample(emoji_pool, k=min(emoji_count, len(emoji_pool)))),
        }

    def _offline_hook(
        self, concept: Concept, rng: random.Random, avoid: Iterable[str] = ()
    ) -> str:
        """Pick a burned-in hook, preferring one not already used in this batch.

        The hook is the first thing a viewer reads. Two videos in a row opening
        with the identical card is the fastest way to look automated.
        """
        hooks: list[str] = list(self.cfg.get("onscreen_hooks", []))
        if not hooks:
            hooks = ["WAIT FOR IT", "POV: {theme}", "RATE THIS 1-10", "WHO DID IT BETTER?"]

        taken = {h.upper() for h in avoid}
        fresh = [h for h in hooks if h.upper() not in taken]
        pool = fresh or hooks  # every hook used already: reuse rather than fail

        fields = self._fields(concept, rng)
        try:
            return rng.choice(pool).format(**fields)
        except KeyError:
            return "WAIT FOR IT"

    def _offline_comment_seed(self, concept: Concept, rng: random.Random) -> str:
        seeds = list(self.cfg.get("comment_seeds", []))
        if not seeds:
            seeds = [
                "Team {animal} or nah? 👀",
                "Drop a 🐾 if you'd adopt {lead}",
                "Which one had the better moves?",
            ]
        fields = self._fields(concept, rng)
        try:
            return rng.choice(seeds).format(**fields)
        except KeyError:
            return "Which one had the better moves?"

    def _alt_text(self, concept: Concept) -> str:
        """Plain description for accessibility.

        Always opens with "AI-animated" — these are synthetic animals and a
        screen-reader user deserves to know that as much as anyone else.
        """
        return truncate(
            f"AI-animated video: {concept.cast_summary} "
            f"{strip_article(concept.dance_label.lower())} "
            f"in {concept.setting_label.lower()}.",
            180,
        )

    def _fallback(self, concept: Concept) -> str:
        return truncate(f"{concept.theme_label} 🐾", self.max_chars)

    # ------------------------------------------------------------------ #
    # Ranking
    # ------------------------------------------------------------------ #
    def rank(
        self,
        candidates: list[str],
        concept: Concept,
        *,
        avoid_shapes: Iterable[str] = (),
    ) -> list[CaptionCandidate]:
        """Score, filter and sort caption candidates, best first."""
        avoid = {s for s in avoid_shapes if s}
        seen: set[str] = set()
        scored: list[CaptionCandidate] = []
        for raw in candidates:
            text = self._tidy(raw)
            key = text.lower()
            if not text or key in seen:
                continue
            if self._violates_safety(text):
                log.warning("caption rejected by safety filter: %r", text,
                            extra={"concept": concept.id})
                continue
            seen.add(key)
            score, reasons = self._score(text)
            if self.shape(text) in avoid:
                # Big enough to lose to a decent alternative, small enough that
                # a repeat still beats an actively bad caption.
                score -= 4.0
                reasons.append("repeat-shape")
            scored.append(CaptionCandidate(text=text, score=score, reasons=reasons))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    def _score(self, text: str) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        for name, (pattern, weight) in _ENGAGEMENT_MARKERS.items():
            if pattern.search(text):
                score += weight
                reasons.append(name)

        # Length: 25-70 chars reads fully in-feed without truncation.
        length = len(text)
        if 25 <= length <= 70:
            score += 2.0
            reasons.append("ideal-length")
        elif length < 18:
            score -= 1.0
            reasons.append("too-short")
        elif length > self.max_chars:
            score -= 2.5
            reasons.append("over-limit")

        emoji_count = len(_EMOJI.findall(text))
        if 1 <= emoji_count <= self.max_emoji:
            score += 1.0
            reasons.append("emoji-ok")
        elif emoji_count > self.max_emoji:
            score -= 1.5
            reasons.append("emoji-heavy")

        if text.isupper():
            score -= 1.0
            reasons.append("all-caps")
        return score, reasons

    def _violates_safety(self, text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in self.banned)

    def _tidy(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip().strip('"')
        text = re.sub(r"\s+([?!.,])", r"\1", text)
        return truncate(text, self.max_chars)

    @staticmethod
    def _clean_hook(hook: str) -> str:
        """On-screen hooks are short, punchy and uppercase-friendly."""
        hook = re.sub(r"\s+", " ", hook).strip().strip('"')
        return truncate(hook, 42)
