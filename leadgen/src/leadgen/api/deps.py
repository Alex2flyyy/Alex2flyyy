"""Shared FastAPI dependencies: auth and serialization helpers."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from leadgen.config import get_settings
from leadgen.db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Guard for write endpoints.

    In development an unset key means open access, which keeps local work
    frictionless. In production an unset key is a configuration error and
    :class:`~leadgen.config.Settings` refuses to boot without one, so reaching
    this code with no key configured is impossible outside development.
    """
    settings = get_settings()
    if not settings.api_key:
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="server misconfigured: no API key set",
            )
        return

    # Constant-time comparison: a plain `!=` leaks key material through
    # response timing.
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )


RequireApiKey = Depends(require_api_key)


def business_payload(business: Any) -> dict[str, Any]:
    from leadgen.enrichment.normalize import format_phone_display

    return {
        "id": business.id,
        "name": business.name,
        "owner_name": business.owner_name,
        "phone": format_phone_display(business.phone_e164) or business.phone,
        "email": business.email,
        "street_address": business.street_address,
        "city": business.city,
        "county": business.county,
        "state": business.state,
        "zip_code": business.zip_code,
        "website_url": business.website_url,
        "website_domain": business.website_domain,
        "google_maps_url": business.google_maps_url,
        "niche_key": business.niche_key,
        "categories": list(business.categories or []),
        "rating": business.rating,
        "review_count": business.review_count,
        "hours": business.hours or {},
        "social_links": business.social_links or {},
        "description": business.description,
        "is_chain": business.is_chain,
        "first_discovered_at": business.first_discovered_at,
        "last_checked_at": business.last_checked_at,
    }


def lead_payload(lead: Any) -> dict[str, Any]:
    return {
        "id": lead.id,
        "business_id": lead.business_id,
        "score": lead.score,
        "previous_score": lead.previous_score,
        "priority": lead.priority,
        "qualified": lead.qualified,
        "status": lead.status.value,
        "website_status": lead.website_status.value,
        "website_status_display": lead.website_status.display,
        "website_score": lead.website_score,
        "reason": lead.reason,
        "contacted_at": lead.contacted_at,
        "follow_up_at": lead.follow_up_at,
        "notes": lead.notes,
        "last_scored_at": lead.last_scored_at,
        "business": business_payload(lead.business),
    }


def audit_payload(audit: Any) -> dict[str, Any]:
    return {
        "id": audit.id,
        "url": audit.url,
        "outcome": audit.outcome.value,
        "score": audit.score,
        "status": audit.status.value,
        "status_display": audit.status.display,
        "uses_https": audit.uses_https,
        "ssl_valid": audit.ssl_valid,
        "mobile_friendly": audit.mobile_friendly,
        "psi_performance": audit.psi_performance,
        "lcp_ms": audit.lcp_ms,
        "design_era": audit.design_era,
        "tech_stack": list(audit.tech_stack or []),
        "problems": list(audit.problems or []),
        "dimension_scores": audit.dimension_scores or {},
        "has_contact_form": audit.has_contact_form,
        "broken_link_count": audit.broken_link_count,
        "screenshot_path": audit.screenshot_path,
        "checked_at": audit.checked_at,
    }
