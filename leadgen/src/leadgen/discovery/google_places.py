"""Google Places API (New) provider.

Primary data source. Chosen over scraping Google Maps for three reasons that
matter more than convenience: it is explicitly licensed, it returns structured
fields (rating, review count, business status) that a scraper has to guess at,
and it will not get your IP blocked mid-run.

Two endpoints are used:

* ``places:searchNearby`` when the niche maps onto Google's type taxonomy.
  Cheaper and more precise, capped at 20 results per call with no pagination.
* ``places:searchText`` otherwise, or when a nearby search comes back full.
  Supports pagination to 60 results across three pages.

**Field masks are mandatory** in the new API and they determine the billing
SKU. The mask here is deliberately minimal — every extra field can move a call
into a pricier tier. Do not add fields casually.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from leadgen.config import Niche, get_settings
from leadgen.discovery.base import Provider, QuotaExceeded
from leadgen.domain import RawBusiness, SearchCell
from leadgen.enrichment.normalize import normalize_state
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Everything the pipeline actually consumes, and nothing else.
SEARCH_FIELD_MASK = ",".join(
    f"places.{f}"
    for f in (
        "id",
        "displayName",
        "formattedAddress",
        "addressComponents",
        "location",
        "rating",
        "userRatingCount",
        "websiteUri",
        "nationalPhoneNumber",
        "googleMapsUri",
        "primaryType",
        "types",
        "businessStatus",
        "priceLevel",
        "regularOpeningHours.weekdayDescriptions",
    )
)

DETAILS_FIELD_MASK = ",".join(
    (
        "id",
        "displayName",
        "formattedAddress",
        "addressComponents",
        "location",
        "rating",
        "userRatingCount",
        "websiteUri",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "googleMapsUri",
        "primaryType",
        "types",
        "businessStatus",
        "priceLevel",
        "regularOpeningHours.weekdayDescriptions",
        "editorialSummary",
    )
)

_COMPONENT_MAP = {
    "street_number": "street_number",
    "route": "route",
    "locality": "city",
    "postal_town": "city",
    "sublocality_level_1": "sublocality",
    "administrative_area_level_2": "county",
    "administrative_area_level_1": "state",
    "postal_code": "zip_code",
    "country": "country",
}


class GooglePlacesProvider(Provider):
    name = "google_places"
    # Nearby Search (New) Pro SKU, ~$32/1000 at time of writing. Verify before
    # you rely on the projection in `leadgen plan`.
    cost_per_call_usd = 0.032
    max_results_per_call = 20

    def __init__(self, fetcher: HttpFetcher) -> None:
        super().__init__(fetcher)
        self.settings = get_settings()
        self.api_key = self.settings.google_maps_api_key
        # Types Google rejected during this run. Google's supported list is not
        # versioned with our config and does change, so rather than trust the
        # catalog we learn from the first rejection and stop resending the type
        # for the remaining cells.
        self._rejected_types: set[str] = set()

    async def available(self) -> bool:
        return bool(self.api_key) and not self._quota_exhausted

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key or "",
            "X-Goog-FieldMask": field_mask,
        }

    async def search(self, cell: SearchCell, niche: Niche, *, limit: int = 20) -> list[RawBusiness]:
        if not await self.available():
            return []

        results: list[RawBusiness] = []
        seen_ids: set[str] = set()

        usable_types = [t for t in niche.place_types if t not in self._rejected_types]
        if usable_types:
            nearby = await self._search_nearby(cell, usable_types, limit=min(limit, 20))
            for business in nearby:
                if business.source_id not in seen_ids:
                    seen_ids.add(business.source_id)
                    results.append(business)

        # Text search fills the gap for niches Google has no type for
        # ("pressure washing"), and tops up when a nearby search came back
        # full, which means we almost certainly missed businesses.
        #
        # An empty result counts as a gap too. Before, a niche that declared a
        # type never reached the keywords, so one type Google no longer accepts
        # -- `general_contractor` -- meant construction returned nothing at all,
        # every cell, every day, while the run still reported success.
        needs_text = not results or len(results) >= self.max_results_per_call
        if needs_text and len(results) < limit:
            for keyword in niche.keywords[:2]:
                if len(results) >= limit:
                    break
                text_results = await self._search_text(cell, keyword, limit=limit - len(results))
                for business in text_results:
                    if business.source_id not in seen_ids:
                        seen_ids.add(business.source_id)
                        business.niche_key = niche.key
                        results.append(business)

        for business in results:
            business.niche_key = niche.key
            business.search_cell = cell.key()
        return results[:limit]

    async def _search_nearby(
        self, cell: SearchCell, place_types: list[str], *, limit: int
    ) -> list[RawBusiness]:
        payload = {
            "includedTypes": place_types,
            "maxResultCount": min(limit, 20),
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": cell.lat, "longitude": cell.lng},
                    "radius": float(cell.radius_m),
                }
            },
            # POPULARITY surfaces established businesses first. DISTANCE would
            # be better for exhaustive coverage, but with a 20-result cap we'd
            # rather have the 20 most substantive businesses in the cell.
            "rankPreference": "POPULARITY",
        }
        data = await self._post(NEARBY_URL, payload, SEARCH_FIELD_MASK)
        return [self._parse(p, cell) for p in data.get("places", []) if p.get("id")]

    async def _search_text(
        self, cell: SearchCell, keyword: str, *, limit: int
    ) -> list[RawBusiness]:
        collected: list[RawBusiness] = []
        page_token: str | None = None

        for _page in range(3):  # API allows at most 3 pages (60 results)
            payload: dict[str, Any] = {
                "textQuery": keyword,
                "maxResultCount": min(limit - len(collected), 20),
                "locationBias": {
                    "circle": {
                        "center": {"latitude": cell.lat, "longitude": cell.lng},
                        "radius": float(cell.radius_m),
                    }
                },
            }
            if page_token:
                payload["pageToken"] = page_token

            mask = SEARCH_FIELD_MASK + ",nextPageToken"
            data = await self._post(TEXT_URL, payload, mask)
            places = data.get("places", [])
            collected.extend(self._parse(p, cell) for p in places if p.get("id"))

            page_token = data.get("nextPageToken")
            if not page_token or len(collected) >= limit:
                break
            # The API rejects a page token used too quickly after issuance.
            await asyncio.sleep(1.5)

        return collected[:limit]

    async def details(self, business: RawBusiness) -> RawBusiness:
        """Fetch the fields the search SKU omits.

        Called selectively — only for businesses that already scored well
        enough to matter — because this is a second billable call per record.
        """
        if not await self.available() or business.source != self.name:
            return business

        url = DETAILS_URL.format(place_id=business.source_id)
        self.api_calls += 1
        response = await self.fetcher.get(url, headers=self._headers(DETAILS_FIELD_MASK))
        if not response.ok:
            self.errors += 1
            return business

        try:
            data = json.loads(response.text)
        except ValueError:
            self.errors += 1
            return business

        if summary := (data.get("editorialSummary") or {}).get("text"):
            business.description = summary
        if phone := data.get("internationalPhoneNumber") or data.get("nationalPhoneNumber"):
            business.phone = business.phone or phone
        if hours := (data.get("regularOpeningHours") or {}).get("weekdayDescriptions"):
            business.hours = _parse_hours(hours)
        return business

    async def _post(self, url: str, payload: dict[str, Any], field_mask: str) -> dict[str, Any]:
        self.api_calls += 1
        response = await self.fetcher.post_json(url, payload, headers=self._headers(field_mask))

        if response.status_code in (401, 403):
            self.errors += 1
            # Google names the actual cause in the body, and the four causes
            # need four different fixes: API_KEY_SERVICE_BLOCKED (the key's API
            # restriction list), SERVICE_DISABLED (Places not enabled on the
            # project), API_KEY_HTTP_REFERRER_BLOCKED (application restriction
            # set to a website), or a billing problem. Guessing between them
            # from the status alone costs a full run per attempt, so report
            # what Google said verbatim.
            detail = " ".join((response.text or "").split())[:400]
            raise QuotaExceeded(
                f"Google Places rejected the request ({response.status_code}). "
                f"Google said: {detail or '(empty response body)'}"
            )
        if response.status_code == 429:
            self._mark_quota_exhausted()
            raise QuotaExceeded("Google Places rate limit / quota exceeded")
        if not response.ok:
            self.errors += 1
            if response.status_code == 400:
                self._remember_rejected_types(response.text)
            log.warning(
                "google_places.request_failed",
                status=response.status_code,
                error=response.error,
                body=response.text[:300],
            )
            return {}

        try:
            return json.loads(response.text) if response.text else {}
        except ValueError:
            self.errors += 1
            return {}

    def _remember_rejected_types(self, body: str) -> None:
        """Pull the type names out of `Unsupported types: a, b` and blacklist them.

        Logged at error level: the niche silently falls back to keyword search,
        which finds fewer businesses, so the catalog entry needs a human fix.
        """
        try:
            message = (json.loads(body or "{}").get("error") or {}).get("message") or ""
        except ValueError:
            return
        marker = "Unsupported types:"
        if marker not in message:
            return
        names = message.split(marker, 1)[1].strip().rstrip(".")
        new = {n.strip() for n in names.split(",") if n.strip()} - self._rejected_types
        if not new:
            return
        self._rejected_types |= new
        log.error(
            "google_places.unsupported_types",
            types=sorted(new),
            hint="remove from config/niches.yml; falling back to keyword search",
        )

    def _parse(self, place: dict[str, Any], cell: SearchCell) -> RawBusiness:
        components = _parse_components(place.get("addressComponents", []))
        location = place.get("location") or {}
        street = " ".join(
            part for part in (components.get("street_number"), components.get("route")) if part
        ).strip()

        types = place.get("types") or []
        primary = place.get("primaryType")
        categories = [primary, *types] if primary else list(types)

        return RawBusiness(
            source=self.name,
            source_id=place["id"],
            name=(place.get("displayName") or {}).get("text", "").strip(),
            phone=place.get("nationalPhoneNumber"),
            website=place.get("websiteUri"),
            street_address=street or None,
            city=components.get("city") or components.get("sublocality") or cell.city,
            state=normalize_state(components.get("state")) or cell.state,
            zip_code=components.get("zip_code") or cell.zip_code,
            country=components.get("country") or "US",
            lat=location.get("latitude"),
            lng=location.get("longitude"),
            google_maps_url=place.get("googleMapsUri"),
            categories=[c.replace("_", " ") for c in categories if c][:10],
            rating=place.get("rating"),
            review_count=place.get("userRatingCount"),
            price_level=_price_level(place.get("priceLevel")),
            hours=_parse_hours((place.get("regularOpeningHours") or {}).get("weekdayDescriptions")),
            business_status=place.get("businessStatus"),
            description=(place.get("editorialSummary") or {}).get("text"),
            search_cell=cell.key(),
            raw={"county": components.get("county") or cell.county},
        )


def _parse_components(components: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for component in components:
        for type_name in component.get("types", []):
            mapped = _COMPONENT_MAP.get(type_name)
            if mapped and mapped not in out:
                out[mapped] = component.get("shortText") or component.get("longText") or ""
    return out


def _parse_hours(descriptions: list[str] | None) -> dict[str, str]:
    """``["Monday: 9 AM - 5 PM", ...]`` -> ``{"Monday": "9 AM - 5 PM"}``"""
    if not descriptions:
        return {}
    hours: dict[str, str] = {}
    for line in descriptions:
        day, _, value = line.partition(":")
        if value:
            hours[day.strip()] = value.strip()
    return hours


def _price_level(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return {
        "PRICE_LEVEL_FREE": 0,
        "PRICE_LEVEL_INEXPENSIVE": 1,
        "PRICE_LEVEL_MODERATE": 2,
        "PRICE_LEVEL_EXPENSIVE": 3,
        "PRICE_LEVEL_VERY_EXPENSIVE": 4,
    }.get(value)
