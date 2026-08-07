"""Concept generation: determinism, coherence, structure and novelty."""

from __future__ import annotations

import datetime as _dt

import pytest

from pawparty.ideas.structure import ROLE_LIMITS, DEFAULT_BANDS, assign_roles, plan_durations
from pawparty.ideas.vocabulary import Option, VocabularyError, VocabularySampler
from pawparty.models import Concept
from pawparty.util import seeded_rng


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_same_seed_produces_identical_concepts(generator):
    a = generator.generate_one(seed=999)
    b = generator.generate_one(seed=999)
    assert a.id == b.id
    assert a.title == b.title
    assert [beat.duration_s for beat in a.beats] == [beat.duration_s for beat in b.beats]


def test_different_seeds_produce_different_concepts(generator):
    a = generator.generate_one(seed=1)
    b = generator.generate_one(seed=2)
    assert a.id != b.id


def test_concept_round_trips_through_json(concept):
    restored = Concept.from_dict(concept.to_dict())
    assert restored.id == concept.id
    assert restored.cast[0].label == concept.cast[0].label
    assert len(restored.beats) == len(concept.beats)
    assert restored.music.bpm == concept.music.bpm


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #
def test_beats_cover_the_target_duration(generator):
    for seed in range(20):
        concept = generator.generate_one(seed=seed)
        total = sum(beat.duration_s for beat in concept.beats)
        assert abs(total - concept.duration_s) < 0.05


def test_every_video_opens_on_a_hook_beat(generator):
    for seed in range(20):
        concept = generator.generate_one(seed=seed)
        assert concept.beats[0].role == "hook"
        assert concept.beats[0].transition_in == "cut", "never open on a transition"


def test_hook_beat_never_drags(generator):
    """A long opening shot is a slow intro, not a hook."""
    ceiling = ROLE_LIMITS["hook"][1]
    for seed in range(30):
        concept = generator.generate_one(seed=seed)
        assert concept.beats[0].duration_s <= ceiling + 0.01


def test_beats_stay_within_generatable_lengths(generator):
    """Beats longer than ~9s exceed what video models can generate in one go."""
    for seed in range(30):
        concept = generator.generate_one(seed=seed)
        for beat in concept.beats:
            assert 0.7 <= beat.duration_s <= 9.01, f"{beat.role} beat is {beat.duration_s}s"


def test_durations_fall_inside_the_requested_range(generator):
    for seed in range(40):
        concept = generator.generate_one(seed=seed)
        assert 8.0 <= concept.duration_s <= 48.0


def test_video_lengths_actually_vary(generator):
    """The brief calls for a mix of lengths, not one length repeated."""
    lengths = {round(generator.generate_one(seed=s).duration_s) for s in range(40)}
    assert len(lengths) >= 12, f"only {len(lengths)} distinct lengths across 40 concepts"


def test_loop_beat_mirrors_the_opening_camera(generator):
    for seed in range(30):
        concept = generator.generate_one(seed=seed)
        last = concept.beats[-1]
        if last.role == "loop" and concept.loop_strategy != "hard_cut":
            assert last.camera == concept.beats[0].camera


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6, 7, 8])
def test_role_assignment_always_has_a_hook_and_an_ending(count):
    roles = assign_roles(count)
    assert len(roles) == count
    assert roles[0] == "hook"
    assert roles[-1] in ("loop", "payoff_loop")
    assert roles.count("hook") == 1


@pytest.mark.parametrize("band", DEFAULT_BANDS)
def test_durations_respect_role_limits_in_every_band(band):
    rng = seeded_rng("limits", band.id)
    for total in (band.min_s, (band.min_s + band.max_s) / 2, band.max_s):
        for count in range(band.beat_count[0], band.beat_count[1] + 1):
            roles = assign_roles(count)
            durations = plan_durations(rng, total, roles)
            for role, value in zip(roles, durations):
                floor, ceiling = ROLE_LIMITS.get(role, (1.5, 8.0))
                assert floor - 0.01 <= value <= ceiling + 0.01, (
                    f"{band.id}/{total}s/{count} beats: {role} got {value}s"
                )


# --------------------------------------------------------------------------- #
# Coherence
# --------------------------------------------------------------------------- #
def test_cast_is_never_empty(generator):
    for seed in range(20):
        assert generator.generate_one(seed=seed).cast


def test_no_two_performers_share_an_accessory(generator):
    for seed in range(40):
        concept = generator.generate_one(seed=seed)
        worn = [a for member in concept.cast for a in member.accessories]
        assert len(worn) == len(set(worn)), f"duplicate accessory in seed {seed}"


def test_no_performer_wears_two_items_in_one_slot(generator, channel):
    """Two hats on one kitten reads as a rendering failure."""
    from pawparty.ideas.concept import ACCESSORY_SLOTS

    tags_by_label = {
        entry.get("label", entry["id"]): set(entry.get("tags", []))
        for entry in channel.vocabulary["accessories"]
    }
    for seed in range(60):
        concept = generator.generate_one(seed=seed)
        for member in concept.cast:
            slots: list[str] = []
            for accessory in member.accessories:
                slots += list(tags_by_label.get(accessory, set()) & ACCESSORY_SLOTS)
            assert len(slots) == len(set(slots)), (
                f"seed {seed}: {member.label} wears two items in one slot: "
                f"{member.accessories}"
            )


def test_option_ids_are_recorded_alongside_labels(generator):
    """The novelty ledger keys on ids; losing them makes it a no-op."""
    concept = generator.generate_one(seed=77)
    for member in concept.cast:
        assert member.animal_id
        assert len(member.accessory_ids) == len(member.accessories)
    assert len(concept.effect_ids) == len(concept.effects)


def test_prompts_are_empty_until_rendered(generator):
    """Concept generation must stay free of prompt rendering (and of cost)."""
    concept = generator.generate_one(seed=3)
    assert all(beat.prompt == "" for beat in concept.beats)


# --------------------------------------------------------------------------- #
# Vocabulary constraints
# --------------------------------------------------------------------------- #
def test_requires_gates_an_option():
    pools = {
        "gear": [
            {"id": "santa_hat", "label": "santa hat", "requires": ["christmas"]},
            {"id": "cap", "label": "cap"},
        ]
    }
    sampler = VocabularySampler(pools, seeded_rng("t"))
    assert [o.id for o in sampler.candidates("gear")] == ["cap"]
    sampler.seed_context("christmas")
    assert {o.id for o in sampler.candidates("gear")} == {"santa_hat", "cap"}


def test_excludes_removes_an_option():
    pools = {"gear": [{"id": "petals", "label": "petals", "excludes": ["cold"]}]}
    sampler = VocabularySampler(pools, seeded_rng("t"))
    sampler.seed_context("cold")
    assert sampler.candidates("gear") == []


def test_picking_absorbs_tags_into_context():
    pools = {"a": [{"id": "x", "label": "x", "tags": ["neon"]}]}
    sampler = VocabularySampler(pools, seeded_rng("t"))
    sampler.pick("a")
    assert "neon" in sampler.context


def test_empty_pool_raises():
    sampler = VocabularySampler({"a": []}, seeded_rng("t"))
    with pytest.raises(VocabularyError):
        sampler.pick("a")


def test_fallback_recovers_from_over_constraint():
    """An over-tight config should degrade, not crash a nightly batch."""
    pools = {"a": [{"id": "x", "label": "x", "excludes": ["cold"]}]}
    sampler = VocabularySampler(pools, seeded_rng("t"))
    sampler.seed_context("cold")
    assert sampler.pick("a", allow_fallback=True).id == "x"


def test_recency_penalty_uses_pool_prefixed_keys():
    """The ledger emits `pool:id`; an unprefixed lookup matches nothing."""
    pools = {
        "themes": [
            {"id": "hot", "label": "hot", "weight": 1.0},
            {"id": "cold", "label": "cold", "weight": 1.0},
        ]
    }
    sampler = VocabularySampler(pools, seeded_rng("t"), recency_penalty={"themes:hot": 0.01})
    picks = [sampler.pick("themes", absorb_tags=False).id for _ in range(60)]
    assert picks.count("cold") > picks.count("hot") * 5


def test_weights_bias_selection():
    pools = {
        "a": [
            {"id": "common", "label": "c", "weight": 10.0},
            {"id": "rare", "label": "r", "weight": 0.1},
        ]
    }
    sampler = VocabularySampler(pools, seeded_rng("weights"))
    picks = [sampler.pick("a", absorb_tags=False).id for _ in range(200)]
    assert picks.count("common") > picks.count("rare") * 5


# --------------------------------------------------------------------------- #
# Seasonality
# --------------------------------------------------------------------------- #
def test_forced_occasion_is_applied(generator):
    concept = generator.generate_one(seed=5, force_occasion="halloween")
    assert concept.occasion == "halloween"


def test_most_content_stays_evergreen(generator):
    """Seasonal bias must not turn the account into a holiday-only feed."""
    december = _dt.date(2026, 12, 10)
    occasions = [generator.generate_one(seed=s, date=december).occasion for s in range(60)]
    seasonal = sum(1 for o in occasions if o != "none")
    assert seasonal < 40, f"{seasonal}/60 videos were seasonal — bias is too high"


# --------------------------------------------------------------------------- #
# Scene coherence
# --------------------------------------------------------------------------- #
def test_lighting_matches_the_time_of_day(generator, channel):
    """Daylight lighting must never land on a night scene, or vice versa.

    Caught in the wild: "dusty sunbeam shafts" on an indoor Disco night. The
    prompt reads as broken to a viewer and confuses the video model.
    """
    lighting_tags = {
        entry.get("label", entry["id"]): (
            set(entry.get("tags", [])), set(entry.get("excludes", []))
        )
        for entry in channel.vocabulary["lighting"]
    }
    theme_tags = {
        entry["id"]: set(entry.get("tags", []))
        for entry in channel.vocabulary["themes"]
    }

    for seed in range(80):
        concept = generator.generate_one(seed=seed)
        excludes = lighting_tags.get(concept.lighting, (set(), set()))[1]
        scene = theme_tags.get(concept.theme, set())
        collision = excludes & scene
        assert not collision, (
            f"seed {seed}: lighting {concept.lighting!r} excludes {collision} "
            f"but theme {concept.theme!r} carries it"
        )


def test_indoor_themes_do_not_get_outdoor_only_settings(generator):
    """A generic fallback setting is fine; a contradictory one is not."""
    for seed in range(60):
        concept = generator.generate_one(seed=seed)
        # Underwater is the sharpest test: nothing dry belongs in a reef.
        if concept.setting == "coral_reef":
            assert concept.theme == "underwater_reef", (
                f"seed {seed}: coral reef setting on theme {concept.theme!r}"
            )
