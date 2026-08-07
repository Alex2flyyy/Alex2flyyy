"""Model schema discovery — turns "read the API tab" into one command.

Wiring a video model by hand means opening its page, reading the input schema,
and working out that what PawParty calls ``first_frame`` this model calls
``start_image``, that its duration is an enum of ``[5, 10]``, and that it wants
``aspect_ratio`` rather than width/height. That is the step people get wrong,
and a wrong ``input_map`` fails at generation time — after you've already
started a batch.

This module fetches the model's real schema and proposes the mapping, flagging
anything it could not resolve. It is advisory: it prints config for you to
check, because a confident wrong guess is worse than a question.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .base import ProviderError, ProviderUnavailable
from .http import request_json

log = get_logger("providers.schema")

REPLICATE_API = "https://api.replicate.com/v1"

#: PawParty field -> candidate names used by real models, best match first.
#: Ordering matters: `image` is a common start-frame name but also a generic
#: reference input, so more specific names are tried first.
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt", "text", "text_prompt", "description"),
    "negative_prompt": ("negative_prompt", "negative", "neg_prompt", "exclude"),
    "duration": ("duration", "duration_seconds", "video_length", "num_seconds", "length"),
    "seed": ("seed", "random_seed", "noise_seed"),
    "first_frame": (
        "start_image", "first_frame", "first_frame_image", "start_frame",
        "image", "input_image", "image_url", "init_image", "reference_image",
    ),
    "width": ("width",),
    "height": ("height",),
    "fps": ("fps", "frames_per_second", "frame_rate"),
    "aspect_ratio": ("aspect_ratio", "aspectratio", "ratio", "resolution_aspect"),
}

#: Values that indicate a model can produce the vertical frame we need.
VERTICAL_HINTS = ("9:16", "portrait", "vertical", "576x1024", "720x1280", "1080x1920")


@dataclass
class ModelSchema:
    """What we learned about a model's inputs."""

    slug: str
    provider: str
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    description: str = ""

    def names(self) -> list[str]:
        return sorted(self.properties)

    def enum_for(self, name: str) -> list[Any] | None:
        prop = self.properties.get(name) or {}
        values = prop.get("enum")
        # Replicate expresses enums via an allOf $ref to a component; the
        # flattener below inlines those, so a plain `enum` is what we see.
        return list(values) if isinstance(values, list) else None


@dataclass
class WiringProposal:
    """A proposed provider config, plus everything we could not resolve."""

    schema: ModelSchema
    input_map: dict[str, str] = field(default_factory=dict)
    static_input: dict[str, Any] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_image_to_video(self) -> bool:
        return "first_frame" in self.input_map

    def to_yaml(self, provider_key: str = "replicate") -> str:
        """The block to paste into settings.yaml (or write to the local file)."""
        lines = [f"    {provider_key}:", f'      model: "{self.schema.slug}"']
        lines.append("      cost_per_second_usd: 0.05   # ← set from the model's pricing page")
        lines.append("      timeout_s: 900")
        lines.append("      input_map:")
        for key, value in self.input_map.items():
            lines.append(f"        {key}: {value}")
        if self.static_input:
            lines.append("      static_input:")
            for key, value in self.static_input.items():
                rendered = f'"{value}"' if isinstance(value, str) else value
                lines.append(f"        {key}: {rendered}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_replicate_schema(slug: str, token: str | None = None) -> ModelSchema:
    """Read a Replicate model's input schema.

    Accepts ``owner/name`` or ``owner/name:versionhash``.
    """
    token = (token or os.getenv("REPLICATE_API_TOKEN", "")).strip()
    if not token:
        raise ProviderUnavailable(
            "REPLICATE_API_TOKEN is not set. Get one at "
            "https://replicate.com/account/api-tokens and put it in .env."
        )

    model_part, _, version = slug.partition(":")
    if model_part.count("/") != 1:
        raise ProviderError(f"Model slug should look like 'owner/name', got {slug!r}")

    headers = {"Authorization": f"Bearer {token}"}
    if version:
        payload = request_json(
            "GET", f"{REPLICATE_API}/models/{model_part}/versions/{version}", headers=headers
        )
        openapi = payload.get("openapi_schema") or {}
        description = payload.get("cog_version", "")
    else:
        payload = request_json("GET", f"{REPLICATE_API}/models/{model_part}", headers=headers)
        latest = payload.get("latest_version") or {}
        openapi = latest.get("openapi_schema") or {}
        description = payload.get("description", "")

    schemas = ((openapi.get("components") or {}).get("schemas") or {})
    input_schema = schemas.get("Input") or {}
    properties = _flatten(input_schema.get("properties") or {}, schemas)

    if not properties:
        raise ProviderError(
            f"Could not read an input schema for {slug!r}. Check the slug is right "
            f"and that the model is public (or that your token can see it)."
        )

    return ModelSchema(
        slug=slug,
        provider="replicate",
        properties=properties,
        required=list(input_schema.get("required") or []),
        description=description,
    )


def _flatten(properties: dict[str, Any], schemas: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Inline Replicate's ``allOf: [{$ref: '#/components/schemas/X'}]`` enums.

    Without this, every enum-typed input looks like an empty object and the
    allowed values (which are exactly what you need for duration and aspect
    ratio) are invisible.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        merged = dict(prop)
        for ref_holder in prop.get("allOf", []) or []:
            ref = (ref_holder or {}).get("$ref", "")
            target = ref.rsplit("/", 1)[-1] if ref else ""
            if target and target in schemas:
                merged = {**schemas[target], **merged}
        merged.pop("allOf", None)
        out[name] = merged
    return out


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def propose_wiring(schema: ModelSchema) -> WiringProposal:
    """Match PawParty's fields onto the model's inputs and flag the gaps."""
    proposal = WiringProposal(schema=schema)
    available = set(schema.properties)
    claimed: set[str] = set()

    for our_field, candidates in FIELD_SYNONYMS.items():
        for candidate in candidates:
            if candidate in available and candidate not in claimed:
                proposal.input_map[our_field] = candidate
                claimed.add(candidate)
                break

    # --- prompt is non-negotiable ------------------------------------------ #
    if "prompt" not in proposal.input_map:
        proposal.warnings.append(
            "No text prompt input found. This model may not be text-driven — "
            "PawParty cannot use it."
        )

    # --- geometry: aspect_ratio and width/height are alternatives ----------- #
    if "aspect_ratio" in proposal.input_map:
        name = proposal.input_map.pop("aspect_ratio")
        vertical = _pick_vertical(schema.enum_for(name))
        if vertical:
            proposal.static_input[name] = vertical
            proposal.notes.append(
                f"{name} is fixed to {vertical!r} in static_input — it never varies per beat."
            )
        else:
            proposal.static_input[name] = "9:16"
            proposal.warnings.append(
                f"Could not confirm {name!r} accepts a vertical value. Set it by hand "
                f"if '9:16' is rejected."
            )
        # With an aspect ratio, explicit pixel dimensions usually conflict.
        for geometry in ("width", "height"):
            proposal.input_map.pop(geometry, None)
        proposal.notes.append(
            "width/height were dropped because the model takes an aspect ratio instead."
        )
    elif {"width", "height"} <= set(proposal.input_map):
        proposal.notes.append("Model takes explicit width/height — PawParty will send 1080x1920.")
    else:
        proposal.warnings.append(
            "Found neither an aspect-ratio nor width/height input. Check the model "
            "can produce vertical video before committing to it — a landscape-only "
            "model costs you the animal's face on every crop."
        )

    # --- duration ----------------------------------------------------------- #
    if "duration" in proposal.input_map:
        name = proposal.input_map["duration"]
        allowed = schema.enum_for(name)
        if allowed:
            proposal.notes.append(
                f"{name} only accepts {allowed}. PawParty requests the nearest value "
                f"and trims to the exact beat length — trimming is free."
            )
    else:
        proposal.warnings.append(
            "No duration input. The model has a fixed clip length; PawParty will "
            "retime each clip to its beat, which is fine but slightly softer."
        )

    # --- unmapped required inputs ------------------------------------------- #
    for name in schema.required:
        if name not in claimed and name not in proposal.static_input:
            prop = schema.properties.get(name, {})
            if "default" in prop:
                continue
            proposal.unmapped.append(name)
            proposal.static_input[name] = prop.get("default", "TODO")

    if proposal.unmapped:
        proposal.warnings.append(
            f"Required input(s) with no PawParty equivalent: {', '.join(proposal.unmapped)}. "
            f"Fill them in under static_input."
        )

    return proposal


def _pick_vertical(enum_values: list[Any] | None) -> str | None:
    """Find the vertical option among a model's allowed aspect ratios."""
    if not enum_values:
        return None
    for value in enum_values:
        if str(value).strip().lower() in ("9:16", "portrait", "vertical"):
            return str(value)
    for value in enum_values:
        if any(hint in str(value).lower() for hint in VERTICAL_HINTS):
            return str(value)
    return None
