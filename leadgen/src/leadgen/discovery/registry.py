"""Provider registry and the multi-provider search orchestrator.

Providers run in the order configured by ``LEADGEN_DISCOVERY_PROVIDERS``. The
orchestrator's job is to combine them without paying twice for the same
business:

* The first provider that returns results for a cell/niche is the primary.
* Later providers are used to *top up* only when the primary came back short
  of the requested limit, which is the case that matters — a full result set
  from Google means adding OSM would mostly return duplicates.
* Quota exhaustion in one provider never aborts the run; the orchestrator drops
  it and continues with the rest.
"""

from __future__ import annotations

from typing import Any

from leadgen.config import Niche, get_settings
from leadgen.discovery.base import Provider, QuotaExceeded
from leadgen.discovery.google_places import GooglePlacesProvider
from leadgen.discovery.openstreetmap import OpenStreetMapProvider
from leadgen.discovery.serpapi import SerpApiProvider
from leadgen.domain import RawBusiness, SearchCell
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "google_places": GooglePlacesProvider,
    "osm": OpenStreetMapProvider,
    "serpapi": SerpApiProvider,
}


def build_providers(fetcher: HttpFetcher, names: list[str] | None = None) -> list[Provider]:
    names = names or get_settings().provider_order
    providers: list[Provider] = []
    for name in names:
        cls = PROVIDER_CLASSES.get(name)
        if cls is None:
            log.warning("provider.unknown", name=name, known=sorted(PROVIDER_CLASSES))
            continue
        providers.append(cls(fetcher))
    return providers


class DiscoveryOrchestrator:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers
        self._disabled: set[str] = set()

    async def usable(self) -> list[Provider]:
        out = []
        for provider in self.providers:
            if provider.name in self._disabled:
                continue
            if await provider.available():
                out.append(provider)
            else:
                log.debug("provider.unavailable", provider=provider.name)
        return out

    async def search_cell(
        self, cell: SearchCell, niche: Niche, *, limit: int = 20
    ) -> list[RawBusiness]:
        collected: list[RawBusiness] = []
        seen_keys: set[str] = set()

        for provider in await self.usable():
            if len(collected) >= limit:
                break
            try:
                results = await provider.search(cell, niche, limit=limit - len(collected))
            except QuotaExceeded as exc:
                log.error("provider.quota", provider=provider.name, error=str(exc))
                self._disabled.add(provider.name)
                continue
            except Exception as exc:
                log.exception(
                    "provider.search_failed",
                    provider=provider.name,
                    cell=cell.key(),
                    niche=niche.key,
                    error=str(exc),
                )
                provider.errors += 1
                continue

            for business in results:
                key = f"{business.source}:{business.source_id}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                collected.append(business)

            log.debug(
                "provider.searched",
                provider=provider.name,
                niche=niche.key,
                cell=cell.label,
                found=len(results),
            )

        return collected[:limit]

    async def enrich_details(self, business: RawBusiness) -> RawBusiness:
        for provider in self.providers:
            if provider.name == business.source:
                try:
                    return await provider.details(business)
                except Exception as exc:
                    log.warning("provider.details_failed", provider=provider.name, error=str(exc))
        return business

    def stats(self) -> dict[str, Any]:
        return {p.name: p.stats() for p in self.providers}

    def total_api_calls(self) -> int:
        return sum(p.api_calls for p in self.providers)
