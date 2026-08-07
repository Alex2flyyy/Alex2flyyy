"""Model schema discovery and the `wire` command.

These use recorded-shape schemas rather than live API calls: the point is that
the mapping logic is right, and a test that needs a network token isn't a test
you can run.
"""

from __future__ import annotations

import pytest

from pawparty.providers.schema import (
    ModelSchema,
    _flatten,
    _pick_vertical,
    propose_wiring,
)


def schema(properties, required=(), slug="owner/model"):
    return ModelSchema(
        slug=slug, provider="replicate", properties=properties, required=list(required)
    )


# --------------------------------------------------------------------------- #
# Schema flattening
# --------------------------------------------------------------------------- #
def test_allof_refs_are_inlined():
    """Replicate hides enum values behind a $ref; unflattened they're invisible."""
    schemas = {"aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]}}
    properties = {
        "aspect_ratio": {
            "allOf": [{"$ref": "#/components/schemas/aspect_ratio"}],
            "description": "Output aspect ratio",
        }
    }
    flat = _flatten(properties, schemas)
    assert flat["aspect_ratio"]["enum"] == ["16:9", "9:16", "1:1"]
    assert "allOf" not in flat["aspect_ratio"]


def test_property_overrides_win_over_the_ref():
    schemas = {"x": {"type": "string", "default": "a"}}
    properties = {"x": {"allOf": [{"$ref": "#/components/schemas/x"}], "default": "b"}}
    assert _flatten(properties, schemas)["x"]["default"] == "b"


def test_missing_ref_is_survivable():
    properties = {"x": {"allOf": [{"$ref": "#/components/schemas/nope"}], "type": "string"}}
    assert _flatten(properties, {})["x"]["type"] == "string"


# --------------------------------------------------------------------------- #
# Vertical detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["16:9", "9:16", "1:1"], "9:16"),
        (["landscape", "portrait"], "portrait"),
        (["720x1280", "1280x720"], "720x1280"),
        (["16:9", "4:3"], None),
        (None, None),
        ([], None),
    ],
)
def test_pick_vertical(values, expected):
    assert _pick_vertical(values) == expected


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def test_straightforward_model_maps_cleanly():
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "duration": {"type": "integer"},
        "seed": {"type": "integer"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    }))
    assert proposal.input_map["prompt"] == "prompt"
    assert proposal.input_map["negative_prompt"] == "negative_prompt"
    assert proposal.input_map["duration"] == "duration"
    assert not proposal.warnings


def test_synonyms_are_resolved():
    proposal = propose_wiring(schema({
        "text": {"type": "string"},
        "num_seconds": {"type": "integer"},
        "start_image": {"type": "string"},
        "random_seed": {"type": "integer"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
    }))
    assert proposal.input_map["prompt"] == "text"
    assert proposal.input_map["duration"] == "num_seconds"
    assert proposal.input_map["first_frame"] == "start_image"
    assert proposal.input_map["seed"] == "random_seed"


def test_specific_start_image_names_beat_generic_image():
    """`image` is also a generic reference input, so it loses to `start_image`."""
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "image": {"type": "string"},
        "start_image": {"type": "string"},
    }))
    assert proposal.input_map["first_frame"] == "start_image"


def test_aspect_ratio_goes_to_static_input_and_drops_width_height():
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    }))
    assert proposal.static_input["aspect_ratio"] == "9:16"
    assert "aspect_ratio" not in proposal.input_map
    assert "width" not in proposal.input_map, "width conflicts with an aspect ratio"
    assert "height" not in proposal.input_map


def test_landscape_only_model_is_flagged():
    """A 16:9-only model costs you the animal's face on every crop."""
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "4:3"]},
    }))
    assert any("vertical" in w.lower() for w in proposal.warnings)


def test_model_with_no_geometry_input_is_flagged():
    proposal = propose_wiring(schema({"prompt": {"type": "string"}}))
    assert any("vertical" in w.lower() for w in proposal.warnings)


def test_missing_prompt_input_is_a_hard_warning():
    proposal = propose_wiring(schema({"image": {"type": "string"}}))
    assert any("prompt" in w.lower() for w in proposal.warnings)


def test_duration_enum_is_surfaced_as_a_note():
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "duration": {"type": "integer", "enum": [5, 10]},
        "aspect_ratio": {"type": "string", "enum": ["9:16"]},
    }))
    assert any("[5, 10]" in n for n in proposal.notes)


def test_missing_duration_is_warned_not_fatal():
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["9:16"]},
    }))
    assert any("duration" in w.lower() for w in proposal.warnings)
    assert proposal.input_map["prompt"] == "prompt"


def test_unmapped_required_inputs_are_surfaced():
    proposal = propose_wiring(schema(
        {
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string", "enum": ["9:16"]},
            "motion_bucket_id": {"type": "integer"},
        },
        required=["prompt", "motion_bucket_id"],
    ))
    assert "motion_bucket_id" in proposal.unmapped
    assert "motion_bucket_id" in proposal.static_input
    assert any("motion_bucket_id" in w for w in proposal.warnings)


def test_required_inputs_with_defaults_are_left_alone():
    proposal = propose_wiring(schema(
        {
            "prompt": {"type": "string"},
            "aspect_ratio": {"type": "string", "enum": ["9:16"]},
            "cfg": {"type": "number", "default": 7.5},
        },
        required=["prompt", "cfg"],
    ))
    assert "cfg" not in proposal.unmapped


def test_image_to_video_is_detected():
    text_only = propose_wiring(schema({
        "prompt": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["9:16"]},
    }))
    img2vid = propose_wiring(schema({
        "prompt": {"type": "string"},
        "start_image": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["9:16"]},
    }))
    assert not text_only.is_image_to_video
    assert img2vid.is_image_to_video


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def test_yaml_block_is_valid_and_round_trips():
    import yaml

    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "duration": {"type": "integer"},
        "start_image": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
    }, slug="owner/cool-model"))

    block = proposal.to_yaml("replicate")
    parsed = yaml.safe_load("providers:\n  options:\n" + block)
    entry = parsed["providers"]["options"]["replicate"]

    assert entry["model"] == "owner/cool-model"
    assert entry["input_map"]["first_frame"] == "start_image"
    assert entry["static_input"]["aspect_ratio"] == "9:16"


def test_proposed_config_is_accepted_by_the_real_provider(monkeypatch):
    """The whole point: what `wire` proposes must actually construct."""
    from pawparty.providers.video.replicate import ReplicateVideoProvider

    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-token")
    proposal = propose_wiring(schema({
        "prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "duration": {"type": "integer", "enum": [5, 10]},
        "start_image": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": ["9:16", "16:9"]},
    }, slug="owner/cool-model"))

    provider = ReplicateVideoProvider({
        "model": proposal.schema.slug,
        "cost_per_second_usd": 0.05,
        "input_map": proposal.input_map,
        "static_input": proposal.static_input,
    })
    provider._check()  # must not raise
    assert provider.input_map["first_frame"] == "start_image"
    assert provider.estimate_cost(4.0) == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# Local settings overlay
# --------------------------------------------------------------------------- #
def test_local_settings_override_the_shared_config(tmp_path, monkeypatch, project_root):
    import shutil

    config_dir = tmp_path / "config"
    shutil.copytree(project_root / "config", config_dir)
    (config_dir / "settings.local.yaml").write_text(
        "concurrency: 9\nproviders:\n  video: replicate\n", encoding="utf-8"
    )
    monkeypatch.setenv("PAWPARTY_WORKSPACE", str(tmp_path / "Videos"))

    from pawparty.config import load_settings

    settings = load_settings(config_dir / "settings.yaml")
    assert settings.concurrency == 9
    assert settings.providers.video == "replicate"
    # Untouched keys still come from settings.yaml.
    assert settings.render.width == 1080


def test_absent_local_settings_is_fine(settings):
    assert settings.providers.video == "synth"
