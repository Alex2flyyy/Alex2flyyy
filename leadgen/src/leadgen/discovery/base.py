"""Discovery provider contract.

A provider takes a :class:`SearchCell` plus a :class:`Niche` and returns
:class:`RawBusiness` records. That is the entire interface. Everything
downstream — dedupe, audit, scoring, AI — is provider-agnostic, so adding a
data source is a single new file plus one registry line.

Providers must:

* Never raise for a single bad result. Log it, skip it, keep going. One
  malformed record must not abort a 4,000-business run.
* Report their own quota usage via :attr:`Provider.api_calls` so the pipeline
  can enforce a budget.
* Be honest in :meth:`Provider.available` about missing credentials, so the
  pipeline can fall back instead of silently returning nothing.
"""

from __future__ import annotations

import abc
from typing import Any

from leadgen.config import Niche
from leadgen.domain import RawBusiness, SearchCell
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)


class ProviderError(Exception):
    """Unrecoverable provider failure — bad credentials, quota exhausted."""


class QuotaExceeded(ProviderError):
    """The provider says we are out of budget. Stop calling it this run."""


class Provider(abc.ABC):
    """Base class for discovery providers."""

    name: str = "base"
    #: Rough per-call cost in USD, used for run cost projection. 0 = free.
    cost_per_call_usd: float = 0.0
    #: Maximum results one call can return, so callers can detect truncation.
    max_results_per_call: int = 20

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher
        self.api_calls = 0
        self.errors = 0
        self._quota_exhausted = False

    @abc.abstractmethod
    async def available(self) -> bool:
        """Whether this provider is usable right now (credentials present)."""

    @abc.abstractmethod
    async def search(self, cell: SearchCell, niche: Niche, *, limit: int = 20) -> list[RawBusiness]:
        """Find businesses of ``niche`` near ``cell``."""

    async def details(self, business: RawBusiness) -> RawBusiness:
        """Optionally enrich one record with a second, more expensive call.

        Default is a no-op. Google implements this to fetch fields that the
        cheap search SKU does not return.
        """
        return business

    @property
    def quota_exhausted(self) -> bool:
        return self._quota_exhausted

    def _mark_quota_exhausted(self) -> None:
        self._quota_exhausted = True
        log.error("provider.quota_exhausted", provider=self.name, calls=self.api_calls)

    def stats(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "api_calls": self.api_calls,
            "errors": self.errors,
            "estimated_cost_usd": round(self.api_calls * self.cost_per_call_usd, 4),
            "quota_exhausted": self._quota_exhausted,
        }


def truncated(results: list[RawBusiness], provider: Provider) -> bool:
    """Whether a search likely hit the provider's result ceiling.

    A full page means there were probably more businesses we never saw, which
    is a signal to shrink ``cell_radius_m`` for that area rather than accept
    silent under-coverage.
    """
    return len(results) >= provider.max_results_per_call
