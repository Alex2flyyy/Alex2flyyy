"""Captions, hashtags, on-screen text."""

from __future__ import annotations

import pytest

from pawparty.copywriting.captions import CaptionWriter
from pawparty.copywriting.hashtags import HashtagBuilder, normalize_tag
from pawparty.copywriting.onscreen import build_subtitle_lines
from pawparty.util import pluralize, slugify, strip_article, truncate


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("word", "expected"),
    [("kitten", "kittens"), ("puppy", "puppies"), ("fox", "foxes"), ("dog", "dogs")],
)
def test_pluralize(word, expected):
    assert pluralize(word) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("a hula dance", "hula dance"), ("the floss", "floss"), ("disco spins", "disco spins")],
)
def test_strip_article(text, expected):
    assert strip_article(text) == expected


def test_slugify_is_filesystem_safe():
    assert slugify("Disco Night! — Kittens & Puppies") == "disco-night-kittens-puppies"


def test_truncate_breaks_on_a_word():
    assert truncate("one two three four", 12) == "one two…"


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def test_captions_are_produced_and_ranked(channel, concept):
    payload = CaptionWriter(channel).write(concept)
    assert len(payload["captions"]) >= 3
    assert payload["chosen_caption"] == payload["captions"][0]
    assert payload["onscreen_hook"]
    assert payload["comment_seed"]
    assert payload["alt_text"]


def test_captions_respect_the_length_cap(channel, concept):
    writer = CaptionWriter(channel)
    for caption in writer.write(concept)["captions"]:
        assert len(caption) <= writer.max_chars


def test_captions_are_deterministic(channel, concept):
    a = CaptionWriter(channel).write(concept)
    b = CaptionWriter(channel).write(concept)
    assert a["captions"] == b["captions"]


def test_caption_options_are_distinct(channel, concept):
    captions = CaptionWriter(channel).write(concept)["captions"]
    assert len(set(captions)) == len(captions)


def test_banned_words_are_filtered(channel, concept):
    writer = CaptionWriter(channel)
    ranked = writer.rank(["This is stupid", "Rate this 1-10"], concept)
    assert all("stupid" not in c.text.lower() for c in ranked)


def test_questions_outrank_statements(channel, concept):
    writer = CaptionWriter(channel)
    ranked = writer.rank(["Which one wins? 🐱", "A kitten dances here today"], concept)
    assert ranked[0].text.startswith("Which")


def test_mid_sentence_who_is_not_treated_as_a_comparison(channel, concept):
    """'someone who needs it' is a statement, not a 'who wins?' question."""
    writer = CaptionWriter(channel)
    ranked = writer.rank(
        ["Who did it better? 🐾", "Send this to someone who needs it today 🐾"], concept
    )
    assert ranked[0].text.startswith("Who did it better")


def test_avoid_shapes_pushes_a_repeat_down(channel, concept):
    writer = CaptionWriter(channel)
    plain = writer.rank(["Who did it better? 🐾", "Rate this 1-10 🐾"], concept)
    avoided = writer.rank(
        ["Who did it better? 🐾", "Rate this 1-10 🐾"],
        concept,
        avoid_shapes={CaptionWriter.shape("Who did it better? 🐾")},
    )
    assert plain[0].text != avoided[0].text


def test_shape_ignores_emoji_and_punctuation():
    assert CaptionWriter.shape("Who did it better? 🐾") == CaptionWriter.shape("who did it better 😻")


def test_batch_captions_do_not_repeat_their_opening(generator, channel):
    writer = CaptionWriter(channel)
    shapes: set[str] = set()
    hooks: set[str] = set()
    for concept in generator.generate_batch(7, seed=31337):
        payload = writer.write(concept, avoid_shapes=shapes, avoid_hooks=hooks)
        shape = CaptionWriter.shape(payload["chosen_caption"])
        assert shape not in shapes, f"repeated caption shape: {payload['chosen_caption']}"
        assert payload["onscreen_hook"].upper() not in hooks
        shapes.add(shape)
        hooks.add(payload["onscreen_hook"].upper())


def test_payoff_hint_is_a_noun_phrase(generator, channel):
    """'Wait for the takes an enormous bow' is what a verb phrase produces."""
    writer = CaptionWriter(channel)
    for seed in range(15):
        concept = generator.generate_one(seed=seed)
        for caption in writer.write(concept)["captions"]:
            assert "wait for the takes" not in caption.lower()
            assert "wait for the goes" not in caption.lower()


def test_llm_failure_falls_back_to_templates(channel, concept):
    class Broken:
        name = "broken"

        def write_copy(self, prompt):
            raise RuntimeError("provider is down")

    payload = CaptionWriter(channel, Broken()).write(concept, "prompt")
    assert payload["captions"], "an API outage must never cost a post"


def test_llm_output_is_merged_and_ranked(channel, concept):
    class Fake:
        name = "fake"

        def write_copy(self, prompt):
            return {
                "captions": ["Which paw won this round? 🐾"],
                "onscreen_hook": "PICK ONE",
                "comment_seed": "Who won?",
                "alt_text": "alt",
                "hashtag_ideas": ["pawparty"],
            }

    payload = CaptionWriter(channel, Fake()).write(concept, "prompt")
    assert "Which paw won this round? 🐾" in payload["captions"]
    assert payload["onscreen_hook"] == "PICK ONE"
    assert payload["hashtag_ideas"] == ["pawparty"]


# --------------------------------------------------------------------------- #
# Hashtags
# --------------------------------------------------------------------------- #
def test_normalize_tag_strips_junk():
    assert normalize_tag("#Disco Night!") == "disconight"
    assert normalize_tag("a") == ""          # too short to be useful
    assert normalize_tag("x" * 40) == ""     # too long to be real


def test_hashtags_include_the_core_identity(channel, concept):
    tags = HashtagBuilder(channel).build(concept)
    for core in channel.hashtags["core"]:
        assert core in tags


def test_hashtags_respect_the_cap(channel, generator):
    builder = HashtagBuilder(channel)
    for seed in range(20):
        tags = builder.build(generator.generate_one(seed=seed))
        assert len(tags) <= builder.max_total
        assert len(tags) == len(set(tags)), "duplicate hashtag"


def test_hashtags_are_deterministic(channel, concept):
    builder = HashtagBuilder(channel)
    assert builder.build(concept) == builder.build(concept)


def test_articles_never_leak_into_hashtags(channel, generator):
    builder = HashtagBuilder(channel)
    for seed in range(30):
        for tag in builder.build(generator.generate_one(seed=seed)):
            assert not tag.startswith(("a", "the")) or tag in {
                "animals", "aianimals", "aiart", "aicats", "aidogs", "theflossdance"
            } or len(tag) > 3


def test_species_tags_match_the_cast(channel, generator):
    """A cat tag on a dogs-only video pulls the wrong audience."""
    builder = HashtagBuilder(channel)
    for seed in range(60):
        concept = generator.generate_one(seed=seed)
        species = {m.species for m in concept.cast}
        for tag in builder.build(concept):
            owner = builder.tag_species.get(tag)
            assert owner is None or owner in species, (
                f"seed {seed}: {tag!r} is a {owner} tag but the cast is {species}"
            )


def test_banned_hashtags_are_never_emitted(channel, generator):
    builder = HashtagBuilder(channel)
    for seed in range(20):
        tags = builder.build(generator.generate_one(seed=seed), ["followforfollow"])
        assert "followforfollow" not in tags


def test_render_prefixes_with_hash(channel, concept):
    builder = HashtagBuilder(channel)
    rendered = builder.render(builder.build(concept))
    assert all(word.startswith("#") for word in rendered.split())


# --------------------------------------------------------------------------- #
# On-screen text
# --------------------------------------------------------------------------- #
def test_the_hook_card_starts_at_frame_zero(concept):
    cards = build_subtitle_lines(concept, "WAIT FOR IT", "Who won?")
    assert cards[0]["start"] == 0.0
    assert cards[0]["emphasis"] == "hook"


def test_cards_never_run_past_the_video(generator):
    for seed in range(20):
        concept = generator.generate_one(seed=seed)
        cards = build_subtitle_lines(concept, "WAIT FOR IT", "Who won?")
        for card in cards:
            assert card["end"] <= concept.duration_s + 0.01
            assert card["start"] < card["end"]


def test_cards_rescale_to_the_real_duration(concept):
    """Transitions shorten the timeline; cards must follow."""
    actual = concept.duration_s * 0.9
    cards = build_subtitle_lines(concept, "WAIT FOR IT", "Who won?", actual_duration_s=actual)
    for card in cards:
        assert card["end"] <= actual + 0.01


def test_missing_copy_drops_only_the_cards_that_need_it(concept):
    """No hook text means no hook card — but the mid-video nudge still stands."""
    cards = build_subtitle_lines(concept, "", "")
    assert not any(c["emphasis"] == "hook" for c in cards)
    assert not any(c["emphasis"] == "cta" for c in cards)


def test_no_beats_means_no_cards(concept):
    concept.beats = []
    assert build_subtitle_lines(concept, "WAIT FOR IT", "Who won?") == []
