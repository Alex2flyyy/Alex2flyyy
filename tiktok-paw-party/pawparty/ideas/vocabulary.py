"""Constrained weighted sampling over the channel vocabulary.

Pure random mixing produces nonsense ("scuba corgi in a snowstorm wearing a
Santa hat at the beach"). Pure hand-authoring doesn't scale to 7 videos a day
forever. The middle path used here is a **tag algebra**:

Every option carries ``tags``, and optionally ``requires`` / ``excludes``:

```yaml
- id: santa_hat
  label: "a tiny Santa hat"
  tags: [holiday, christmas, headwear]
  requires: [christmas]     # only offered once the context is Christmassy
  excludes: [underwater]    # never offered in an underwater setting
  weight: 1.0
```

Sampling walks the pools in a fixed order (theme -> setting -> ... ), and each
pick contributes its tags to a growing context. Later pools only see options
whose ``requires`` are satisfied and whose ``excludes`` don't collide. The
result: combinatorially huge, but always coherent.

Bias controls (``recency_penalty``) let the novelty ledger push the sampler away
from options used in recent videos without ever hard-banning them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..util import weighted_choice


class VocabularyError(RuntimeError):
    """Raised when constraints eliminate every option in a pool."""


@dataclass(frozen=True)
class Option:
    """One selectable vocabulary entry."""

    id: str
    label: str
    tags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    weight: float = 1.0
    #: Free-form extras consumed by prompt templates (bpm, prompt fragments…).
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], pool: str) -> "Option":
        if "id" not in raw:
            raise VocabularyError(f"Option in pool {pool!r} is missing an 'id': {raw!r}")
        known = {"id", "label", "tags", "requires", "excludes", "weight"}
        return cls(
            id=str(raw["id"]),
            label=str(raw.get("label", raw["id"].replace("_", " "))),
            tags=tuple(raw.get("tags", []) or []),
            requires=tuple(raw.get("requires", []) or []),
            excludes=tuple(raw.get("excludes", []) or []),
            weight=float(raw.get("weight", 1.0)),
            data={k: v for k, v in raw.items() if k not in known},
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def is_compatible(self, context_tags: Iterable[str]) -> bool:
        ctx = set(context_tags)
        if self.requires and not set(self.requires).issubset(ctx):
            return False
        if self.excludes and set(self.excludes) & ctx:
            return False
        return True


class VocabularySampler:
    """Draws options from channel pools while accumulating a tag context."""

    def __init__(
        self,
        pools: dict[str, list[dict[str, Any]]],
        rng: random.Random,
        *,
        recency_penalty: dict[str, float] | None = None,
        min_weight: float = 0.02,
    ) -> None:
        self._pools: dict[str, list[Option]] = {
            name: [Option.from_dict(entry, name) for entry in entries or []]
            for name, entries in pools.items()
        }
        self.rng = rng
        #: ``"<pool>:<option_id>"`` -> multiplier in (0, 1]; 1.0 means "not seen
        #: recently". The pool prefix matters: the novelty ledger stores keys in
        #: that form, and an unprefixed lookup silently matches nothing, which
        #: would make the whole anti-repetition system a no-op.
        self.recency_penalty = recency_penalty or {}
        self.min_weight = min_weight
        self.context: set[str] = set()
        self.picked: dict[str, list[Option]] = {}

    # -- context ------------------------------------------------------------ #
    def seed_context(self, *tags: str) -> None:
        """Force tags into the context before sampling (e.g. a forced holiday)."""
        self.context.update(t for t in tags if t)

    def candidates(self, pool: str) -> list[Option]:
        options = self._pools.get(pool)
        if options is None:
            raise VocabularyError(f"No vocabulary pool named {pool!r}")
        return [o for o in options if o.is_compatible(self.context)]

    # -- sampling ----------------------------------------------------------- #
    def _weights(self, pool: str, options: Sequence[Option]) -> list[float]:
        weights = []
        for option in options:
            penalty = self.recency_penalty.get(f"{pool}:{option.id}", 1.0)
            weights.append(max(self.min_weight, option.weight * penalty))
        return weights

    def pick(
        self,
        pool: str,
        *,
        absorb_tags: bool = True,
        required_tag: str | None = None,
        forbid_ids: Iterable[str] = (),
        allow_fallback: bool = True,
    ) -> Option:
        """Pick one option from ``pool``.

        ``allow_fallback`` relaxes ``required_tag`` (then the tag context) before
        giving up, so a slightly over-constrained config degrades to a sensible
        pick instead of crashing a nightly batch.
        """
        forbidden = set(forbid_ids)
        options = [o for o in self.candidates(pool) if o.id not in forbidden]
        if required_tag:
            tagged = [o for o in options if required_tag in o.tags]
            if tagged:
                options = tagged
            elif not allow_fallback:
                raise VocabularyError(
                    f"Pool {pool!r} has no option tagged {required_tag!r} in this context"
                )

        if not options and allow_fallback:
            options = [o for o in self._pools.get(pool, []) if o.id not in forbidden]
        if not options:
            raise VocabularyError(
                f"Pool {pool!r} produced no candidates (context={sorted(self.context)})"
            )

        choice = weighted_choice(self.rng, options, self._weights(pool, options))
        if absorb_tags:
            self.context.update(choice.tags)
        self.picked.setdefault(pool, []).append(choice)
        return choice

    def pick_many(
        self,
        pool: str,
        count: int,
        *,
        absorb_tags: bool = True,
        distinct: bool = True,
    ) -> list[Option]:
        """Pick ``count`` options, re-filtering after each pick.

        Re-filtering matters: choosing "sunglasses" may unlock or forbid later
        accessories, and we want that to apply within a single multi-pick.
        """
        chosen: list[Option] = []
        taken: set[str] = set()
        for _ in range(max(0, count)):
            try:
                option = self.pick(
                    pool,
                    absorb_tags=absorb_tags,
                    forbid_ids=taken if distinct else (),
                    allow_fallback=False,
                )
            except VocabularyError:
                break
            chosen.append(option)
            taken.add(option.id)
        return chosen

    def maybe(self, pool: str, probability: float, **kwargs: Any) -> Option | None:
        """Pick from ``pool`` with the given probability, else ``None``."""
        if self.rng.random() < probability:
            try:
                return self.pick(pool, **kwargs)
            except VocabularyError:
                return None
        return None

    def by_id(self, pool: str, option_id: str) -> Option | None:
        for option in self._pools.get(pool, []):
            if option.id == option_id:
                return option
        return None

    def pool_size(self, pool: str) -> int:
        return len(self._pools.get(pool, []))

    def has_pool(self, pool: str) -> bool:
        return bool(self._pools.get(pool))

    @property
    def pool_names(self) -> list[str]:
        return sorted(self._pools)

    def combination_space(self, pools: Sequence[str]) -> int:
        """Upper bound on distinct combinations across ``pools`` (docs/CLI use)."""
        total = 1
        for pool in pools:
            total *= max(1, self.pool_size(pool))
        return total
