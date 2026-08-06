"""The anti-repetition machinery — the thing that keeps the account watchable."""

from __future__ import annotations

import pytest

from pawparty.ideas.novelty import NoveltyConfig, NoveltyLedger


def test_similarity_is_jaccard():
    assert NoveltyLedger.similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert NoveltyLedger.similarity({"a", "b"}, {"c", "d"}) == 0.0
    assert NoveltyLedger.similarity({"a", "b"}, {"a", "c"}) == pytest.approx(1 / 3)


def test_exact_duplicate_is_rejected(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    options = ["themes:disco", "settings:floor", "animals:corgi"]
    ledger.record("ch", "c1", "sig", options)
    ok, reason = ledger.is_novel("ch", options)
    assert not ok
    assert "duplicate" in reason


def test_near_duplicate_is_rejected(tmp_path):
    """One swapped hat is not a new video."""
    ledger = NoveltyLedger(tmp_path / "n.sqlite3", NoveltyConfig(max_similarity=0.7))
    base = [f"o{i}" for i in range(10)]
    ledger.record("ch", "c1", "sig", base)
    ok, reason = ledger.is_novel("ch", base[:9] + ["different"])
    assert not ok
    assert "similar" in reason


def test_genuinely_different_concept_passes(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    ledger.record("ch", "c1", "sig", [f"a{i}" for i in range(10)])
    ok, _ = ledger.is_novel("ch", [f"b{i}" for i in range(10)])
    assert ok


def test_channels_do_not_share_history(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    options = ["themes:disco"]
    ledger.record("cats", "c1", "sig", options)
    ok, _ = ledger.is_novel("dogs", options)
    assert ok, "a cats-only video must not block an identical dogs video"


# --------------------------------------------------------------------------- #
# Recency penalty
# --------------------------------------------------------------------------- #
def test_just_used_options_are_heavily_penalised(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3", NoveltyConfig(penalty_strength=0.8))
    ledger.record("ch", "c1", "sig", ["themes:disco"])
    penalties = ledger.recency_penalty("ch")
    assert penalties["themes:disco"] < 0.25


def test_penalty_decays_as_videos_accumulate(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3", NoveltyConfig(decay_window=10))
    ledger.record("ch", "c0", "sig", ["themes:disco"])
    first = ledger.recency_penalty("ch")["themes:disco"]

    for i in range(1, 6):
        ledger.record("ch", f"c{i}", "sig", [f"themes:other{i}"])
    later = ledger.recency_penalty("ch")["themes:disco"]

    assert later > first, "penalty should relax as the option ages"


def test_penalty_expires_past_the_window(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3", NoveltyConfig(decay_window=5))
    ledger.record("ch", "c0", "sig", ["themes:disco"])
    for i in range(1, 8):
        ledger.record("ch", f"c{i}", "sig", [f"themes:other{i}"])
    assert "themes:disco" not in ledger.recency_penalty("ch")


def test_penalty_keys_keep_the_pool_prefix(tmp_path):
    """Sampler lookups use `pool:id`; bare ids would silently never match."""
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    ledger.record("ch", "c1", "sig", ["themes:disco", "animals:corgi"])
    assert set(ledger.recency_penalty("ch")) == {"themes:disco", "animals:corgi"}


def test_re_recording_does_not_age_the_library(tmp_path):
    """A resumed build re-commits its concept; that must not advance the clock."""
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    ledger.record("ch", "c1", "sig", ["themes:disco"])
    before = ledger.recency_penalty("ch")["themes:disco"]
    ledger.record("ch", "c1", "sig", ["themes:disco"])
    assert ledger.recency_penalty("ch")["themes:disco"] == before
    assert ledger.count("ch") == 1


def test_forget_removes_a_concept(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    options = ["themes:disco"]
    ledger.record("ch", "c1", "sig", options)
    ledger.forget("c1")
    ok, _ = ledger.is_novel("ch", options)
    assert ok
    assert ledger.count("ch") == 0


def test_leaderboard_counts_usage(tmp_path):
    ledger = NoveltyLedger(tmp_path / "n.sqlite3")
    for i in range(3):
        ledger.record("ch", f"c{i}", "sig", ["themes:disco", f"animals:a{i}"])
    top = dict(ledger.option_leaderboard("ch"))
    assert top["themes:disco"] == 3


# --------------------------------------------------------------------------- #
# Integration with the generator
# --------------------------------------------------------------------------- #
def test_a_batch_contains_no_duplicate_concepts(generator):
    concepts = generator.generate_batch(7, seed=4242)
    assert len({c.id for c in concepts}) == 7
    assert len({c.signature for c in concepts}) == 7


def test_a_batch_varies_its_themes(generator):
    """Seven videos a day that share a theme look like one video posted seven times."""
    concepts = generator.generate_batch(7, seed=808)
    themes = [c.theme for c in concepts]
    assert len(set(themes)) >= 6, f"only {len(set(themes))} distinct themes: {themes}"


def test_consecutive_batches_keep_diverging(generator):
    first = generator.generate_batch(7, seed=11)
    second = generator.generate_batch(7, seed=22)
    overlap = {c.theme for c in first} & {c.theme for c in second}
    assert len(overlap) <= 3, f"day two repeated {len(overlap)} themes from day one"


def test_generated_concepts_are_committed(generator, ledger, channel):
    """Committing per concept is what makes a single batch varied."""
    generator.generate_batch(3, seed=55)
    assert ledger.count(channel.name) == 3
