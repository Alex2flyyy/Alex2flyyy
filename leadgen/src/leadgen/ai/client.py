"""Anthropic API client wrapper.

Adds the four things a batch job needs that the raw SDK does not provide:

* **A token budget.** ``LEADGEN_AI_DAILY_TOKEN_BUDGET`` is enforced in-process.
  An unbounded loop over 5,000 businesses is a four-figure surprise; the budget
  turns that into a logged, graceful degradation.
* **Bounded concurrency**, so a burst of enrichment doesn't hit rate limits.
* **Retry with backoff** on 429 and 5xx only.
* **Tool-use extraction**, so responses are validated structures rather than
  prose we have to parse with regexes.

Enrichment is always optional. Every caller must work when
``LEADGEN_AI_ENABLED=false`` or no key is set — the pipeline's core value
(discovery, audit, scoring) does not depend on an LLM.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from leadgen.config import get_settings
from leadgen.logging import get_logger

log = get_logger(__name__)


class AIUnavailable(Exception):
    """AI is disabled, unconfigured, or out of budget."""


class BudgetExhausted(AIUnavailable):
    pass


@dataclass(slots=True)
class AIResponse:
    tool_input: dict[str, Any]
    text: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AIClient:
    def __init__(self, *, model: str | None = None, concurrency: int | None = None) -> None:
        self.settings = get_settings()
        self.model = model or self.settings.ai_model
        self.premium_model = self.settings.ai_model_premium
        self._semaphore = asyncio.Semaphore(concurrency or self.settings.ai_max_concurrency)
        self._client: Any = None
        self.tokens_used = 0
        self.calls = 0
        self.errors = 0
        self.budget = self.settings.ai_daily_token_budget

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ai_enabled and self.settings.anthropic_api_key)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.budget - self.tokens_used)

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not self.enabled:
                raise AIUnavailable("AI enrichment is disabled or ANTHROPIC_API_KEY is not set")
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    async def call_tool(
        self,
        *,
        system: str,
        prompt: str,
        tool: dict[str, Any],
        max_tokens: int = 1500,
        temperature: float = 0.3,
        premium: bool = False,
        max_attempts: int = 3,
    ) -> AIResponse:
        """Make one tool-use call and return the validated tool input.

        ``tool_choice`` forces the tool, so the model cannot answer in prose and
        leave us parsing free text.
        """
        if self.tokens_used >= self.budget:
            raise BudgetExhausted(
                f"AI token budget exhausted ({self.tokens_used:,}/{self.budget:,})"
            )

        client = self._ensure_client()
        model = self.premium_model if premium else self.model

        async with self._semaphore:
            for attempt in range(1, max_attempts + 1):
                try:
                    message = await client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system,
                        messages=[{"role": "user", "content": prompt}],
                        tools=[tool],
                        tool_choice={"type": "tool", "name": tool["name"]},
                    )
                except Exception as exc:
                    if not _retryable(exc) or attempt >= max_attempts:
                        self.errors += 1
                        raise AIUnavailable(f"Anthropic call failed: {exc}") from exc
                    delay = min(2**attempt, 16) * (0.5 + random.random())
                    log.warning(
                        "ai.retry", attempt=attempt, delay=round(delay, 1), error=str(exc)[:200]
                    )
                    await asyncio.sleep(delay)
                    continue

                self.calls += 1
                usage = getattr(message, "usage", None)
                input_tokens = getattr(usage, "input_tokens", 0) or 0
                output_tokens = getattr(usage, "output_tokens", 0) or 0
                self.tokens_used += input_tokens + output_tokens

                tool_input: dict[str, Any] = {}
                text_parts: list[str] = []
                for block in message.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_input = dict(block.input)  # type: ignore[arg-type]
                    elif getattr(block, "type", None) == "text":
                        text_parts.append(block.text)  # type: ignore[attr-defined]

                if not tool_input:
                    self.errors += 1
                    raise AIUnavailable(
                        f"model returned no tool call (stop_reason={message.stop_reason})"
                    )

                return AIResponse(
                    tool_input=tool_input,
                    text="\n".join(text_parts),
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    stop_reason=message.stop_reason,
                )

        raise AIUnavailable("exhausted retries")

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "tokens_used": self.tokens_used,
            "budget_remaining": self.budget_remaining,
            "model": self.model,
        }


def _retryable(exc: Exception) -> bool:
    """Retry only on transient failures.

    A 400 means our schema is wrong and will be wrong again; a 401 means the
    key is bad. Retrying either wastes time and hides the real error.
    """
    name = type(exc).__name__
    if name in {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"}:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)
