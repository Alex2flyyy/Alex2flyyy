"""AI enrichment service.

Turns audit facts into the sales artifacts a human actually uses: a business
summary, the opportunity stated as business impact, prioritized redesign
recommendations, and an outreach angle.

Two cost controls make this affordable at volume:

* **Input hashing.** The hash of everything that fed a summary is stored with
  it. On the next run, an unchanged business is skipped entirely. In steady
  state most of the database is unchanged night to night, so this eliminates
  the large majority of calls.
* **Ranked gating.** Only the top N scored leads get enriched. Writing a
  bespoke pitch for lead #4,000 that nobody will ever call is pure waste.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from leadgen.ai.client import AIClient, AIUnavailable, BudgetExhausted
from leadgen.ai.prompts import (
    CATEGORIZE_TOOL,
    ENRICHMENT_TOOL,
    SYSTEM_PROMPT,
    build_categorize_prompt,
    build_enrichment_prompt,
)
from leadgen.config import Niche, get_niches
from leadgen.domain import AIEnrichment, WebsiteAudit
from leadgen.logging import get_logger

log = get_logger(__name__)


def compute_input_hash(
    *,
    name: str,
    website: str | None,
    website_status: str,
    website_score: float | None,
    problems: list[str],
    review_count: int | None,
) -> str:
    """Fingerprint of everything that would change the analysis.

    The website score is bucketed to the nearest 5 points so trivial drift —
    a 200ms difference in response time — does not trigger a re-analysis that
    produces the same words.
    """
    bucket = round((website_score or 0) / 5) * 5
    basis = json.dumps(
        {
            "name": name,
            "website": website,
            "status": website_status,
            "score_bucket": bucket,
            "problems": sorted(problems)[:10],
            "reviews_bucket": round((review_count or 0) / 10) * 10,
        },
        sort_keys=True,
    )
    return hashlib.sha256(basis.encode()).hexdigest()


class EnrichmentService:
    def __init__(self, client: AIClient | None = None) -> None:
        self.client = client or AIClient()
        self.enriched = 0
        self.skipped = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    async def enrich(
        self,
        *,
        name: str,
        niche: Niche | None,
        city: str | None,
        state: str | None,
        categories: list[str],
        rating: float | None,
        review_count: int | None,
        website: str | None,
        audit: WebsiteAudit | None,
        content_excerpt: str | None = None,
        has_phone: bool = False,
        hours_published: bool = False,
        premium: bool = False,
    ) -> AIEnrichment | None:
        if not self.enabled:
            return None

        website_status = (
            audit.status.display if audit else ("No Website" if not website else "Unknown")
        )
        prompt = build_enrichment_prompt(
            name=name,
            niche_label=niche.label if niche else "Local business",
            niche_expectation=niche.web_expectation if niche else "clear services, contact info",
            city=city,
            state=state,
            categories=categories,
            rating=rating,
            review_count=review_count,
            website=website,
            website_status=website_status,
            website_score=audit.score if audit else None,
            problems=audit.problems if audit else [],
            tech_stack=audit.tech_stack if audit else [],
            design_era=audit.design_era if audit else None,
            page_title=audit.title if audit else None,
            meta_description=audit.meta_description if audit else None,
            content_excerpt=content_excerpt,
            has_contact_form=bool(audit and audit.has_contact_form),
            has_phone=has_phone,
            hours_published=hours_published,
        )

        try:
            response = await self.client.call_tool(
                system=SYSTEM_PROMPT,
                prompt=prompt,
                tool=ENRICHMENT_TOOL,
                max_tokens=1400,
                premium=premium,
            )
        except BudgetExhausted:
            raise
        except AIUnavailable as exc:
            self.failed += 1
            log.warning("ai.enrich_failed", business=name, error=str(exc)[:200])
            return None

        data = response.tool_input
        self.enriched += 1
        return AIEnrichment(
            summary=str(data.get("summary", ""))[:500],
            opportunity=str(data.get("opportunity", ""))[:600],
            redesign_recommendations=[
                str(r)[:200] for r in (data.get("redesign_recommendations") or [])
            ][:5],
            outreach_angle=str(data.get("outreach_angle", ""))[:500],
            outreach_subject=str(data.get("outreach_subject", ""))[:200],
            talking_points=[str(t)[:200] for t in (data.get("talking_points") or [])][:4],
            inferred_niche=(
                str(data["inferred_niche"])[:60] if data.get("inferred_niche") else None
            ),
            estimated_value_tier=data.get("estimated_value_tier"),
            confidence=float(data.get("confidence") or 0.0),
            model=response.model,
            tokens_used=response.total_tokens,
        )

    async def enrich_many(
        self, items: list[dict[str, Any]], *, concurrency: int | None = None
    ) -> list[AIEnrichment | None]:
        """Enrich a batch, preserving input order.

        A budget exhaustion stops further work but returns everything already
        completed, so a partial run still delivers value.
        """
        if not self.enabled:
            return [None] * len(items)

        semaphore = asyncio.Semaphore(concurrency or self.client.settings.ai_max_concurrency)
        stop = asyncio.Event()

        async def one(item: dict[str, Any]) -> AIEnrichment | None:
            if stop.is_set():
                return None
            async with semaphore:
                try:
                    return await self.enrich(**item)
                except BudgetExhausted:
                    stop.set()
                    log.error("ai.budget_exhausted", completed=self.enriched)
                    return None
                except Exception as exc:
                    self.failed += 1
                    log.warning("ai.enrich_error", error=str(exc)[:200])
                    return None

        return list(await asyncio.gather(*(one(item) for item in items)))

    async def categorize(
        self, businesses: list[dict[str, Any]], *, batch_size: int = 25
    ) -> dict[int, tuple[str, float]]:
        """Assign niches to businesses whose category is ambiguous.

        Returns ``{original_index: (niche_key, confidence)}``. Used for records
        from providers with weak taxonomies, where "Establishment" is the only
        category returned.
        """
        if not self.enabled or not businesses:
            return {}

        valid_keys = set(get_niches().keys)
        out: dict[int, tuple[str, float]] = {}

        for offset in range(0, len(businesses), batch_size):
            batch = businesses[offset : offset + batch_size]
            try:
                response = await self.client.call_tool(
                    system=SYSTEM_PROMPT,
                    prompt=build_categorize_prompt(batch, sorted(valid_keys)),
                    tool=CATEGORIZE_TOOL,
                    max_tokens=1200,
                    temperature=0.0,
                )
            except AIUnavailable as exc:
                log.warning("ai.categorize_failed", error=str(exc)[:200])
                break

            for assignment in response.tool_input.get("assignments", []):
                try:
                    local_index = int(assignment["index"])
                    niche_key = str(assignment["niche_key"])
                    confidence = float(assignment.get("confidence", 0))
                except (KeyError, TypeError, ValueError):
                    continue
                # Guard against a hallucinated niche key silently corrupting
                # the niche column for thousands of rows.
                if niche_key in valid_keys and 0 <= local_index < len(batch):
                    out[offset + local_index] = (niche_key, confidence)

        return out

    def stats(self) -> dict[str, Any]:
        return {
            "enriched": self.enriched,
            "skipped_cached": self.skipped,
            "failed": self.failed,
            **self.client.stats(),
        }
