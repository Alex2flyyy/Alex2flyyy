"""Configuration loading, validation and channel inheritance."""

from __future__ import annotations

import pytest
import yaml

from pawparty.config import ConfigError, list_channels, load_channel, load_settings


def test_settings_load(settings):
    assert settings.render.width == 1080
    assert settings.render.height == 1920
    assert settings.schedule.videos_per_day >= 1
    assert settings.workspace.name == "Videos"


def test_env_overrides_workspace(tmp_path, monkeypatch, project_root):
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(target))
    loaded = load_settings(project_root / "config" / "settings.yaml")
    assert loaded.workspace == target


def test_env_overrides_budget(tmp_path, monkeypatch, project_root):
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("PAWPARTY_DAILY_BUDGET_USD", "1.25")
    loaded = load_settings(project_root / "config" / "settings.yaml")
    assert loaded.budget.daily_usd == 1.25


def test_horizontal_render_is_rejected(tmp_path, monkeypatch, project_root):
    """A 16:9 config would silently produce unpostable video."""
    config_dir = tmp_path / "config"
    (config_dir / "channels").mkdir(parents=True)
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump({"render": {"width": 1920, "height": 1080}}), encoding="utf-8"
    )
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(tmp_path / "Videos"))
    with pytest.raises(ConfigError, match="vertical"):
        load_settings(config_dir / "settings.yaml")


def test_unknown_key_is_rejected(tmp_path, monkeypatch):
    """Typos in settings.yaml should fail loudly, not be ignored."""
    config_dir = tmp_path / "config"
    (config_dir / "channels").mkdir(parents=True)
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump({"render": {"framerate": 30}}), encoding="utf-8"
    )
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(tmp_path / "Videos"))
    with pytest.raises(ConfigError, match="framerate"):
        load_settings(config_dir / "settings.yaml")


def test_unknown_channel_lists_alternatives(settings):
    with pytest.raises(ConfigError, match="kittens_puppies"):
        load_channel(settings, "does_not_exist")


def test_channel_has_every_required_pool(settings):
    required = {
        "themes", "settings", "palettes", "animals", "accessories",
        "dance_styles", "camera_moves", "transitions", "effects", "music_vibes",
    }
    for name in list_channels(settings):
        profile = load_channel(settings, name)
        missing = required - set(profile.vocabulary)
        assert not missing, f"channel {name} is missing pools: {missing}"
        for pool in required:
            assert profile.vocabulary[pool], f"channel {name} pool {pool} is empty"


def test_every_option_has_an_id(settings):
    for name in list_channels(settings):
        profile = load_channel(settings, name)
        for pool, entries in profile.vocabulary.items():
            for entry in entries:
                assert "id" in entry, f"{name}.{pool} has an option with no id: {entry}"


def test_option_ids_are_unique_within_a_pool(settings):
    for name in list_channels(settings):
        profile = load_channel(settings, name)
        for pool, entries in profile.vocabulary.items():
            ids = [e["id"] for e in entries]
            duplicates = {i for i in ids if ids.count(i) > 1}
            assert not duplicates, f"{name}.{pool} has duplicate ids: {duplicates}"


def test_camera_move_ids_match_implemented_motions(settings):
    """A camera move in YAML with no implementation silently degrades."""
    from pawparty.media.motion import MOTIONS

    for name in list_channels(settings):
        profile = load_channel(settings, name)
        for entry in profile.vocabulary["camera_moves"]:
            assert entry["id"] in MOTIONS, (
                f"{name}: camera move {entry['id']!r} has no implementation in "
                f"pawparty/media/motion.py"
            )


def test_transition_ids_match_implemented_transitions(settings):
    from pawparty.media.assemble import TRANSITIONS

    for name in list_channels(settings):
        profile = load_channel(settings, name)
        for entry in profile.vocabulary["transitions"]:
            assert entry["id"] in TRANSITIONS, (
                f"{name}: transition {entry['id']!r} is not implemented"
            )


# --------------------------------------------------------------------------- #
# Inheritance
# --------------------------------------------------------------------------- #
def test_channel_inheritance_pulls_in_parent_vocabulary(settings):
    child = load_channel(settings, "cats_only")
    parent = load_channel(settings, "kittens_puppies")
    assert child.vocabulary["themes"] == parent.vocabulary["themes"]
    assert child.display_name == "Kitten Club"


def test_vocabulary_remove_prunes_inherited_entries(settings):
    child = load_channel(settings, "cats_only")
    species = {a.get("species") for a in child.vocabulary["animals"]}
    assert species == {"kitten"}, "cats_only should contain no dogs"


def test_child_overrides_replace_parent_values(settings):
    child = load_channel(settings, "cats_only")
    parent = load_channel(settings, "kittens_puppies")
    assert child.hashtags["core"] != parent.hashtags["core"]
    # Un-overridden keys still come through from the parent.
    assert child.hashtags["broad"] == parent.hashtags["broad"]


def test_circular_inheritance_is_detected(settings, tmp_path):
    channels = settings.channels_dir
    a, b = channels / "_test_a.yaml", channels / "_test_b.yaml"
    try:
        a.write_text("extends: _test_b\n", encoding="utf-8")
        b.write_text("extends: _test_a\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Circular"):
            load_channel(settings, "_test_a")
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)
