"""ConceptGenerator — turns a seed into a complete, novel, coherent video spec.

The generation order is deliberate. Each pool is sampled only after the picks
that could constrain it, so tags flow downhill:

    occasion -> theme -> setting -> palette -> lighting -> cast -> accessories
             -> dance style -> camera moves -> transitions -> effects -> music

Nothing here touches the network or the disk, so concept generation is fast
enough to over-generate and filter: the novelty ledger can reject a candidate
and we simply try again with a derived seed.
"""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any

from ..config import ChannelProfile, Settings
from ..logging_setup import get_logger
from ..models import Beat, CastMember, Concept, MusicSpec
from ..util import pluralize, seeded_rng, short_hash, slugify, strip_article
from . import structure as struct
from .novelty import NoveltyLedger
from .vocabulary import Option, VocabularySampler

log = get_logger("ideas")

#: Tags that behave as mutually-exclusive body slots when dressing a performer.
ACCESSORY_SLOTS = {
    "headwear",
    "eyewear",
    "face_gear",
    "neckwear",
    "body_wear",
    "paw_wear",
}

#: Pools whose ids define a concept's identity for novelty purposes.
SIGNATURE_POOLS = (
    "themes",
    "settings",
    "dance_styles",
    "animals",
    "accessories",
    "palettes",
    "music_vibes",
    "effects",
)

#: Month -> occasion tag, used to bias seasonal content automatically.
SEASONAL_HINTS = {
    1: "new_year", 2: "valentines", 3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "halloween",
    11: "autumn", 12: "christmas",
}


class ConceptGenerator:
    """Builds :class:`~pawparty.models.Concept` objects for a channel."""

    def __init__(
        self,
        settings: Settings,
        channel: ChannelProfile,
        ledger: NoveltyLedger | None = None,
    ) -> None:
        self.settings = settings
        self.channel = channel
        self.ledger = ledger
        self.bands = struct.load_bands(channel.structure)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def generate_batch(
        self,
        count: int,
        *,
        seed: int | None = None,
        date: _dt.date | None = None,
        force_occasion: str | None = None,
        max_attempts_per_concept: int = 12,
    ) -> list[Concept]:
        """Generate ``count`` mutually distinct concepts.

        Each accepted concept is committed to the ledger immediately, before the
        next one is generated. That is what makes a *single* batch varied: the
        recency penalty from video 1 is already steering the sampler when
        video 2 is drawn. Generating all seven first and committing at the end
        leaves the sampler blind within the day, which is exactly when
        repetition is most visible to a viewer scrolling your profile.

        Distinctness is enforced twice over: against the ledger (all history)
        and against the batch in progress (belt and braces — the batch check
        still catches duplicates if the ledger is unavailable).
        """
        base_seed = seed if seed is not None else random.SystemRandom().getrandbits(32)
        date = date or _dt.date.today()
        batch: list[Concept] = []
        batch_options: list[set[str]] = []

        for slot in range(count):
            concept = None
            for attempt in range(max_attempts_per_concept):
                candidate_seed = (base_seed + slot * 10_000 + attempt * 137) % (2**32)
                candidate = self.generate_one(
                    seed=candidate_seed, date=date, force_occasion=force_occasion,
                    slot=slot,
                )
                options = set(self._option_ids(candidate))

                ok, reason = (True, "")
                if self.ledger is not None:
                    ok, reason = self.ledger.is_novel(self.channel.name, sorted(options))
                if ok:
                    for previous in batch_options:
                        score = NoveltyLedger.similarity(options, previous)
                        limit = (self.ledger.config.max_similarity if self.ledger else 0.72)
                        if score > limit:
                            ok, reason = False, f"{score:.0%} similar to another concept in this batch"
                            break
                if ok:
                    concept = candidate
                    batch_options.append(options)
                    break
                log.debug(
                    "concept rejected (%s), retrying", reason,
                    extra={"concept": candidate.id, "attempt": attempt + 1},
                )

            if concept is None:
                # Every attempt collided. Rather than fail the night's batch we
                # accept the last candidate and flag it — a near-duplicate is a
                # far better outcome than a missing post.
                concept = candidate  # type: ignore[assignment]
                concept.notes.append(
                    "novelty: accepted after exhausting retries; consider widening the vocabulary"
                )
                batch_options.append(set(self._option_ids(concept)))
                log.warning(
                    "accepted a low-novelty concept after %d attempts — add more vocabulary options",
                    max_attempts_per_concept,
                    extra={"concept": concept.id},
                )

            self.commit(concept)
            batch.append(concept)

        return batch

    def generate_one(
        self,
        *,
        seed: int,
        date: _dt.date | None = None,
        force_occasion: str | None = None,
        slot: int = 0,
    ) -> Concept:
        """Build a single concept from a seed. Fully deterministic."""
        date = date or _dt.date.today()
        rng = seeded_rng(self.channel.name, seed, slot)

        penalties = self.ledger.recency_penalty(self.channel.name) if self.ledger else {}
        sampler = VocabularySampler(self.channel.vocabulary, rng, recency_penalty=penalties)

        occasion = self._pick_occasion(sampler, rng, date, force_occasion)
        theme = sampler.pick("themes")
        setting = sampler.pick("settings")
        palette = sampler.pick("palettes")
        lighting = sampler.pick("lighting") if sampler.pool_size("lighting") else None
        cast = self._build_cast(sampler, rng)
        dance = sampler.pick("dance_styles")
        cameras = self._pick_cameras(sampler, rng)
        transitions = self._pick_transitions(sampler, rng)
        effects = sampler.pick_many("effects", rng.choice([1, 1, 2, 2, 3]))
        music = sampler.pick("music_vibes")

        band = struct.choose_band(rng, self.bands)
        total_s = struct.total_from_band(rng, band)
        beat_count = struct.beat_count_for(total_s, band)

        hook_option = sampler.pick("hooks") if sampler.has_pool("hooks") else None
        payoff_option = sampler.pick("payoffs") if sampler.has_pool("payoffs") else None
        loop_strategy = rng.choice(
            self.channel.structure.get(
                "loop_strategies", ["crossfade_to_start", "freeze_hold", "crossfade_to_start"]
            )
        )

        actions = self._beat_actions(rng, cast, dance, theme, payoff_option)
        beats = struct.build_beats(
            rng,
            total_s=total_s,
            beat_count=beat_count,
            actions=actions,
            cameras=cameras,
            transitions=transitions,
            loop_strategy=loop_strategy,
        )

        title = self._title(theme, cast, dance, occasion)
        concept_id = f"{slugify(title, 34)}-{short_hash(self.channel.name, seed, slot, title)}"

        concept = Concept(
            id=concept_id,
            seed=seed,
            channel=self.channel.name,
            title=title,
            theme=theme.id,
            theme_label=theme.label,
            cast=cast,
            dance_style=dance.id,
            dance_label=dance.label,
            setting=setting.id,
            setting_label=setting.label,
            palette=palette.id,
            palette_label=palette.label,
            lighting=lighting.label if lighting else "bright even key light",
            effects=[e.label for e in effects],
            effect_ids=[e.id for e in effects],
            music=MusicSpec(
                vibe=music.id,
                label=music.label,
                bpm=int(music.get("bpm", rng.choice([100, 112, 120, 128, 136]))),
                energy=str(music.get("energy", "high")),
                tags=list(music.tags),
            ),
            hook=hook_option.label if hook_option else "an impossible-to-scroll-past first frame",
            payoff=payoff_option.label if payoff_option else "the whole crew nails the final pose",
            payoff_hint=(
                str(payoff_option.get("hint", "ending")) if payoff_option else "ending"
            ),
            loop_strategy=loop_strategy,
            duration_s=round(sum(b.duration_s for b in beats), 2),
            beats=beats,
            occasion=occasion,
        )
        concept.notes.append(f"duration band: {band.label} — {band.notes}")
        concept.signature = short_hash(*sorted(self._option_ids(concept)), length=12)
        return concept

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _pick_occasion(
        self,
        sampler: VocabularySampler,
        rng: random.Random,
        date: _dt.date,
        forced: str | None,
    ) -> str:
        """Seasonal content wins when it's timely, but never dominates.

        `seasonal_bias` (0-1) is the chance that a video leans into the current
        month's occasion. The rest stay evergreen so the library keeps working
        year-round.
        """
        if forced:
            option = sampler.by_id("occasions", forced)
            sampler.seed_context(*(option.tags if option else (forced,)))
            return forced
        if not sampler.pool_size("occasions"):
            return "none"

        bias = float(self.channel.structure.get("seasonal_bias", 0.22))
        hint = SEASONAL_HINTS.get(date.month)
        if hint and rng.random() < bias:
            option = sampler.by_id("occasions", hint)
            if option:
                sampler.seed_context(*option.tags)
                sampler.picked.setdefault("occasions", []).append(option)
                return option.id
        if rng.random() < bias * 0.5:
            option = sampler.pick("occasions")
            return option.id
        return "none"

    def _build_cast(self, sampler: VocabularySampler, rng: random.Random) -> list[CastMember]:
        """1-3 performers. Duos out-perform solos on comment volume (`who wins?`)."""
        weights = self.channel.structure.get("cast_size_weights", {"1": 0.3, "2": 0.5, "3": 0.2})
        sizes = [int(k) for k in weights]
        size = rng.choices(sizes, weights=[float(weights[str(s)]) for s in sizes], k=1)[0]

        animals = sampler.pick_many("animals", size, distinct=True)
        if not animals:
            animals = [sampler.pick("animals")]

        # No accessory is worn by two performers at once — matching outfits read
        # as a rendering bug rather than a costume choice.
        used_ids: set[str] = set()

        cast: list[CastMember] = []
        for animal in animals:
            accessory_count = rng.choices([0, 1, 2], weights=[0.15, 0.55, 0.30], k=1)[0]
            accessories = self._pick_accessories(sampler, accessory_count, used_ids)
            personality = sampler.pick("personalities") if sampler.has_pool("personalities") else None
            cast.append(
                CastMember(
                    species=str(animal.get("species", animal.id.split("_")[0])),
                    breed=str(animal.get("breed", animal.label)),
                    label=animal.label,
                    personality=personality.label if personality else "delighted and slightly chaotic",
                    accessories=[a.label for a in accessories],
                    animal_id=animal.id,
                    accessory_ids=[a.id for a in accessories],
                )
            )
        return cast

    @staticmethod
    def _pick_accessories(
        sampler: VocabularySampler, count: int, used_ids: set[str]
    ) -> list[Option]:
        """Pick ``count`` accessories for one performer, one item per body slot.

        Slots come from the accessory's own tags (``headwear``, ``eyewear``, …),
        so the rule is data-driven: tag a new accessory with its slot and it
        automatically stops competing with the others in that slot. Two hats on
        one kitten is the kind of detail that makes generated video look broken.
        """
        taken_slots: set[str] = set()
        chosen: list[Option] = []

        for _ in range(count):
            # Over-sample, then take the first candidate whose slot is free.
            candidates = sampler.pick_many(
                "accessories", count + 4, distinct=True, absorb_tags=False
            )
            pick = None
            for option in candidates:
                if option.id in used_ids:
                    continue
                slots = set(option.tags) & ACCESSORY_SLOTS
                if slots & taken_slots:
                    continue
                pick = option
                taken_slots |= slots
                break
            if pick is None:
                break
            used_ids.add(pick.id)
            chosen.append(pick)
            # Absorb tags only for the accessory we actually kept, so context
            # reflects the costume rather than everything we considered.
            sampler.context.update(pick.tags)
        return chosen

    def _pick_cameras(
        self, sampler: VocabularySampler, rng: random.Random
    ) -> list[tuple[str, str]]:
        """One camera move per possible beat, never repeating twice in a row."""
        moves = sampler.pick_many("camera_moves", 8, distinct=True, absorb_tags=False)
        if not moves:
            moves = [Option(id="static", label="locked-off shot")]
        rng.shuffle(moves)
        return [(m.id, str(m.get("prompt", m.label))) for m in moves]

    def _pick_transitions(self, sampler: VocabularySampler, rng: random.Random) -> list[str]:
        picks = sampler.pick_many("transitions", 6, distinct=True, absorb_tags=False)
        ids = [p.id for p in picks] or ["cut"]
        rng.shuffle(ids)
        return ids

    def _beat_actions(
        self,
        rng: random.Random,
        cast: list[CastMember],
        dance: Option,
        theme: Option,
        payoff: Option | None,
    ) -> dict[str, str]:
        """Role -> action sentence. Concrete verbs beat adjectives for video models."""
        lead = cast[0].label
        crew = " and ".join(m.label for m in cast)
        move = str(dance.get("move", dance.label))
        signature = str(dance.get("signature_move", f"a {dance.label} spin"))
        payoff_text = payoff.label if payoff else "throws its paws up in a triumphant finish"

        return {
            "hook": (
                f"{lead} is already mid-{move}, facing camera, eyes wide with excitement, "
                f"paws bouncing on the beat from the very first frame"
            ),
            "build": (
                f"{crew} step into {move}, tails wagging, heads bobbing in sync, "
                f"gaining energy with each beat"
            ),
            "switch": (
                f"the beat drops and {crew} snap into {signature}, "
                f"a completely different rhythm from a fresh angle"
            ),
            "payoff": f"{crew} {payoff_text}, wearing an unmistakably delighted expression",
            "payoff_loop": f"{crew} {payoff_text}, then settles straight back into the opening pose",
            "loop": (
                f"{lead} returns to the exact opening pose, {move} restarting seamlessly "
                f"in the {theme.label} scene"
            ),
        }

    def _title(
        self, theme: Option, cast: list[CastMember], dance: Option, occasion: str
    ) -> str:
        species = sorted({m.species for m in cast})
        who = " & ".join(pluralize(s).capitalize() for s in species)
        if occasion == "none":
            subject = theme.label
        else:
            # "Halloween haunted mansion", not "Halloween Haunted mansion".
            subject = (
                f"{occasion.replace('_', ' ').title()} "
                f"{theme.label[0].lower()}{theme.label[1:]}"
            )
        return f"{subject} — {who} {strip_article(dance.label)}".strip()

    def _option_ids(self, concept: Concept) -> list[str]:
        """Signature option ids, recovered from the concept itself.

        Deriving these from the concept (rather than from the live sampler)
        means a concept loaded back from JSON can still be novelty-checked.
        Keys are ``"<pool>:<option_id>"`` — the exact form
        :meth:`VocabularySampler._weights` looks up.
        """
        ids = [
            f"themes:{concept.theme}",
            f"settings:{concept.setting}",
            f"dance_styles:{concept.dance_style}",
            f"palettes:{concept.palette}",
            f"music_vibes:{concept.music.vibe}",
        ]
        ids += [f"animals:{m.animal_id or slugify(m.label)}" for m in concept.cast]
        ids += [
            f"accessories:{accessory_id}"
            for m in concept.cast
            for accessory_id in (m.accessory_ids or [slugify(a) for a in m.accessories])
        ]
        ids += [
            f"effects:{effect_id}"
            for effect_id in (concept.effect_ids or [slugify(e) for e in concept.effects])
        ]
        if concept.occasion != "none":
            ids.append(f"occasions:{concept.occasion}")
        return ids

    def signature_options(self, concept: Concept) -> list[str]:
        return sorted(set(self._option_ids(concept)))

    def commit(self, concept: Concept) -> None:
        """Record a concept in the novelty ledger once it is accepted."""
        if self.ledger is None:
            return
        self.ledger.record(
            self.channel.name,
            concept.id,
            concept.signature,
            self.signature_options(concept),
            concept.title,
        )

    # -- introspection ------------------------------------------------------ #
    def combination_estimate(self) -> dict[str, Any]:
        """Rough size of the creative space — reported by `pawparty channels`."""
        sampler = VocabularySampler(self.channel.vocabulary, random.Random(0))
        sizes = {pool: sampler.pool_size(pool) for pool in sampler.pool_names}
        core = sampler.combination_space(
            ["themes", "settings", "dance_styles", "palettes", "music_vibes", "camera_moves"]
        )
        # Cast and accessories multiply this substantially; report conservatively.
        animals = max(1, sizes.get("animals", 1))
        accessories = max(1, sizes.get("accessories", 1))
        cast_space = animals * (animals - 1) // 2 if animals > 1 else 1
        return {
            "pool_sizes": sizes,
            "core_combinations": core,
            "with_cast_and_accessories": core * max(1, cast_space) * accessories,
        }
