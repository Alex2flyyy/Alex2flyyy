"""API request and response models.

Separate from both the ORM and the domain dataclasses on purpose: the wire
format is a public contract, and it should not change just because a column
was renamed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from leadgen.domain import LeadStatus, WebsiteStatus


class BusinessOut(BaseModel):
    id: int
    name: str
    owner_name: str | None = None
    phone: str | None = None
    email: str | None = None
    street_address: str | None = None
    city: str | None = None
    county: str | None = None
    state: str | None = None
    zip_code: str | None = None
    website_url: str | None = None
    website_domain: str | None = None
    google_maps_url: str | None = None
    niche_key: str | None = None
    categories: list[str] = Field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    hours: dict[str, Any] = Field(default_factory=dict)
    social_links: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    is_chain: bool = False
    first_discovered_at: datetime | None = None
    last_checked_at: datetime | None = None


class AuditOut(BaseModel):
    id: int
    url: str
    outcome: str
    score: float
    status: str
    status_display: str
    uses_https: bool
    ssl_valid: bool
    mobile_friendly: bool | None = None
    psi_performance: float | None = None
    lcp_ms: float | None = None
    design_era: str | None = None
    tech_stack: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    has_contact_form: bool = False
    broken_link_count: int = 0
    screenshot_path: str | None = None
    checked_at: datetime


class InsightOut(BaseModel):
    summary: str | None = None
    opportunity: str | None = None
    redesign_recommendations: list[str] = Field(default_factory=list)
    outreach_angle: str | None = None
    outreach_subject: str | None = None
    talking_points: list[str] = Field(default_factory=list)
    estimated_value_tier: str | None = None
    confidence: float = 0.0
    model: str | None = None


class ContactOut(BaseModel):
    kind: str
    value: str
    label: str | None = None
    person_name: str | None = None
    is_primary: bool = False
    is_generic: bool = False
    confidence: float = 0.0


class LeadOut(BaseModel):
    id: int
    business_id: int
    score: int
    previous_score: int | None = None
    priority: int
    qualified: bool
    status: str
    website_status: str
    website_status_display: str
    website_score: float | None = None
    reason: str | None = None
    contacted_at: datetime | None = None
    follow_up_at: datetime | None = None
    notes: str | None = None
    last_scored_at: datetime | None = None
    business: BusinessOut


class LeadDetailOut(LeadOut):
    score_components: list[dict[str, Any]] = Field(default_factory=list)
    score_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    latest_audit: AuditOut | None = None
    insight: InsightOut | None = None
    contacts: list[ContactOut] = Field(default_factory=list)


class LeadPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[LeadOut]


class LeadUpdateIn(BaseModel):
    status: LeadStatus | None = None
    notes: str | None = None
    follow_up_at: datetime | None = None
    channel: str | None = Field(default=None, max_length=40)
    deal_value: float | None = None
    lost_reason: str | None = Field(default=None, max_length=200)


class LeadFilters(BaseModel):
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)
    statuses: list[LeadStatus] | None = None
    website_statuses: list[WebsiteStatus] | None = None
    niches: list[str] | None = None
    cities: list[str] | None = None
    states: list[str] | None = None
    zips: list[str] | None = None
    qualified_only: bool = False
    has_phone: bool | None = None
    has_email: bool | None = None
    discovered_on: date | None = None
    discovered_since: date | None = None
    query: str | None = None
    order_by: str = "score"
    limit: int = Field(default=50, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class RunOut(BaseModel):
    id: int
    run_date: date
    trigger: str
    status: str
    location_key: str | None = None
    niche_keys: list[str] = Field(default_factory=list)
    discovered: int
    new_businesses: int
    duplicates: int
    audited: int
    scored: int
    qualified: int
    enriched: int
    errors: list[Any] = Field(default_factory=list)
    stages: list[Any] = Field(default_factory=list)
    api_calls: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None


class RunRequest(BaseModel):
    location: str = "home_base_25mi"
    niches: list[str] = Field(default_factory=list)
    target_leads: int = Field(default=50, ge=1, le=10000)
    use_ai: bool = True
    use_browser: bool | None = None
    use_pagespeed: bool = True
    discovery_cap: int | None = Field(default=None, ge=1, le=100_000)
    dry_run: bool = False


class ExportRequest(BaseModel):
    destination: str = "csv"
    crm: str = "generic"
    filters: LeadFilters = Field(default_factory=LeadFilters)


class ExportOut(BaseModel):
    destination: str
    status: str
    row_count: int
    output_path: str | None = None
    remote_ref: str | None = None
    error: str | None = None


class SuppressionIn(BaseModel):
    kind: str = Field(pattern="^(email|phone|domain|business)$")
    value: str = Field(min_length=1, max_length=400)
    reason: str | None = Field(default=None, max_length=300)


class StatsOut(BaseModel):
    overview: dict[str, Any]
    by_city: list[dict[str, Any]]
    by_niche: list[dict[str, Any]]
    by_website_status: list[dict[str, Any]]
    by_status: list[dict[str, Any]]
    score_distribution: list[dict[str, Any]]
    daily_trend: list[dict[str, Any]]
    funnel: list[dict[str, Any]]


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    environment: str
    providers: list[str]
    ai_enabled: bool
