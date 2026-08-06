"""Renders concept objects into the exact strings sent to generation providers.

Templates live in ``pawparty/prompts/templates/*.j2`` and are plain Jinja2, so
prompt engineering is a text edit — no Python change, no redeploy. A channel can
override any template by dropping a same-named file in
``config/prompt_overrides/<channel>/``.

Every rendered prompt is written to ``Videos/Prompts/<date>/<concept>/`` so you
can always answer "what exactly did we send?" months later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from ..config import ChannelProfile, Settings
from ..models import Beat, Concept

#: Collapse the whitespace Jinja leaves behind; providers charge by token.
_WS = re.compile(r"[ \t]*\n[ \t]*")
_MULTI_SPACE = re.compile(r" {2,}")

DEFAULT_STYLE = {
    "render_style": (
        "hyper-cute 3D animated short film look, Pixar-quality fur shading, "
        "soft subsurface scattering, glossy highlights"
    ),
    "quality_anchor": "crisp 4K detail, cinematic depth, professional colour grade",
    "base_negatives": "ugly, deformed, disfigured, low quality",
}


class PromptRenderer:
    """Turns a :class:`Concept` into provider-ready prompt strings."""

    def __init__(self, settings: Settings, channel: ChannelProfile) -> None:
        self.settings = settings
        self.channel = channel

        builtin = Path(__file__).parent / "templates"
        override = settings.project_root / "config" / "prompt_overrides" / channel.name
        loaders = [FileSystemLoader(str(override))] if override.exists() else []
        loaders.append(FileSystemLoader(str(builtin)))

        self.env = Environment(
            loader=ChoiceLoader(loaders),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
            autoescape=False,
        )
        self.style = {**DEFAULT_STYLE, **(channel.prompt_style or {})}

    # ------------------------------------------------------------------ #
    def render_all(self, concept: Concept) -> Concept:
        """Fill in ``prompt`` / ``negative_prompt`` / ``still_prompt`` on beats.

        Mutates and returns the concept so callers can chain.
        """
        negative = self.render_negative(concept)
        for beat in concept.beats:
            beat.prompt = self.render_beat(concept, beat)
            beat.still_prompt = self.render_still(concept, beat)
            beat.negative_prompt = negative
        return concept

    def render_beat(self, concept: Concept, beat: Beat) -> str:
        return self._render("video_beat.j2", self._context(concept, beat))

    def render_still(self, concept: Concept, beat: Beat) -> str:
        context = self._context(concept, beat)
        context["frozen_action"] = self._freeze(beat.action)
        return self._render("still_beat.j2", context)

    def render_negative(self, concept: Concept) -> str:
        extra = self.channel.safety.get("negative_prompt_extra", "")
        base = self.style.get("base_negatives", DEFAULT_STYLE["base_negatives"])
        if extra:
            base = f"{base}, {extra}"
        return self._render("negative.j2", {"base_negatives": base})

    def render_music(self, concept: Concept) -> str:
        return self._render(
            "music.j2",
            {
                "music": concept.music,
                "theme_description": self._theme_description(concept),
                "duration": int(concept.duration_s) + 2,
            },
        )

    def render_copy_prompt(self, concept: Concept) -> str:
        copy_cfg = self.channel.copy or {}
        return self._render(
            "copy_llm.j2",
            {
                "channel": self.channel,
                "concept": concept,
                "voice_rules": copy_cfg.get("voice_rules", []),
                "caption_count": int(copy_cfg.get("caption_count", 5)),
                "max_caption_chars": int(copy_cfg.get("max_caption_chars", 90)),
                "max_hook_chars": int(copy_cfg.get("max_hook_chars", 42)),
                "max_emoji": int(copy_cfg.get("max_emoji", 2)),
            },
        )

    # ------------------------------------------------------------------ #
    def _render(self, template_name: str, context: dict[str, Any]) -> str:
        try:
            template = self.env.get_template(template_name)
        except TemplateNotFound as exc:  # pragma: no cover - config error path
            raise FileNotFoundError(f"Prompt template not found: {template_name}") from exc
        text = template.render(**context)
        text = _WS.sub(" ", text)
        text = _MULTI_SPACE.sub(" ", text)
        return text.strip()

    def _context(self, concept: Concept, beat: Beat) -> dict[str, Any]:
        return {
            "concept": concept,
            "beat": beat,
            "music": concept.music,
            "style": self.style,
            "cast_description": self._cast_description(concept),
            "theme_description": self._theme_description(concept),
        }

    def _cast_description(self, concept: Concept) -> str:
        parts = []
        for member in concept.cast:
            bits = [f"an adorable {member.breed} {member.species}"]
            if member.accessories:
                bits.append(f"wearing {' and '.join(member.accessories)}")
            bits.append(f"({member.personality})")
            parts.append(" ".join(bits))
        return " and ".join(parts)

    def _theme_description(self, concept: Concept) -> str:
        bits = [concept.theme_label]
        if concept.occasion and concept.occasion != "none":
            bits.append(f"{concept.occasion.replace('_', ' ')} celebration")
        return ", ".join(bits)

    @staticmethod
    def _freeze(action: str) -> str:
        """Rewrite a motion sentence into a single frozen moment for stills."""
        frozen = action.replace("is already mid-", "caught mid-")
        frozen = frozen.replace("step into", "frozen mid-")
        frozen = frozen.replace("snap into", "captured at the peak of")
        return frozen
