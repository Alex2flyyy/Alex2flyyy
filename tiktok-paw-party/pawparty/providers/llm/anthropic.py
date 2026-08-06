"""Anthropic LLM provider — fresh caption/hook copy per video.

Called once per video (a few hundred tokens), so it is by far the cheapest part
of the pipeline. Uses the Messages API over plain HTTP to avoid pinning an SDK
version inside an otherwise dependency-light project.

Configure::

    providers:
      llm: anthropic
      options:
        anthropic:
          model: "claude-sonnet-5"
          max_tokens: 900
          temperature: 1.0

Requires ``ANTHROPIC_API_KEY``. Any failure is caught by the caption writer,
which falls back to offline templates — an API blip never costs you a post.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ...logging_setup import get_logger
from ..base import BaseProvider, ProviderError, ProviderUnavailable
from ..http import request_json

log = get_logger("providers.anthropic")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

#: Rough Sonnet-class pricing for the budget estimator (USD per 1M tokens).
_INPUT_PER_MTOK = 3.0
_OUTPUT_PER_MTOK = 15.0

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class AnthropicLLMProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.model = str(self.opt("model", "claude-sonnet-5"))
        self.max_tokens = int(self.opt("max_tokens", 900))
        self.temperature = float(self.opt("temperature", 1.0))

    def estimate_cost(self) -> float:
        # ~700 in / ~300 out for a caption request.
        return round(700 / 1e6 * _INPUT_PER_MTOK + 300 / 1e6 * _OUTPUT_PER_MTOK, 5)

    def _check(self) -> None:
        """Called by the registry so a missing key degrades at startup."""
        if not self.api_key:
            raise ProviderUnavailable(
                "ANTHROPIC_API_KEY is not set — using the offline copywriter instead."
            )

    def write_copy(self, prompt: str) -> dict[str, Any]:
        self._check()

        payload = request_json(
            "POST",
            API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )

        blocks = payload.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            raise ProviderError("Anthropic returned an empty response")

        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Tolerate fenced code blocks and surrounding prose."""
        candidate = text
        if "```" in candidate:
            parts = candidate.split("```")
            candidate = max(parts, key=len).lstrip("json").strip()
        match = _JSON_BLOCK.search(candidate)
        if not match:
            raise ProviderError(f"Could not find JSON in LLM response: {text[:200]}")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"LLM returned malformed JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProviderError("LLM JSON was not an object")
        return data
