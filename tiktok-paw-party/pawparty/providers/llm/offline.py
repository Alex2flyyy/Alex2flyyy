"""`offline` LLM provider — a no-op that hands control back to the templates.

The caption writer already produces good, ranked, deterministic copy from the
channel's ``caption_patterns``. This provider exists so the LLM slot is never
``None`` in the registry and the pipeline has one code path.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseProvider


class OfflineLLMProvider(BaseProvider):
    name = "offline"

    def estimate_cost(self) -> float:
        return 0.0

    def write_copy(self, prompt: str) -> dict[str, Any]:  # noqa: ARG002
        """Returns nothing, which makes CaptionWriter fall through to templates."""
        return {}
