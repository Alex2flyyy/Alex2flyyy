"""Domain types shared across layers.

These are transport objects, deliberately independent of both the ORM and the
HTTP schemas. A discovery provider produces :class:`RawBusiness`; the auditor
produces :class:`WebsiteAudit`; the scorer produces :class:`ScoreResult`. Only
the persistence layer knows about SQLAlchemy, and only the API layer knows
about response models. That separation is what lets you add a provider or swap
the database without touching the scoring logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


# =============================================================================
# Enumerations
# =============================================================================


class WebsiteStatus(StrEnum):
    NONE = "no_website"
    BROKEN = "broken"
    POOR = "poor"
    OUTDATED = "outdated"
    AVERAGE = "average"
    GOOD = "good"
    UNKNOWN = "unknown"

    @property
    def is_opportunity(self) -> bool:
        return self in {
            WebsiteStatus.NONE,
            WebsiteStatus.BROKEN,
            WebsiteStatus.POOR,
            WebsiteStatus.OUTDATED,
        }

    @property
    def display(self) -> str:
        return {
            WebsiteStatus.NONE: "No Website",
            WebsiteStatus.BROKEN: "Broken Website",
            WebsiteStatus.POOR: "Poor Website",
            WebsiteStatus.OUTDATED: "Outdated Website",
            WebsiteStatus.AVERAGE: "Average Website",
            WebsiteStatus.GOOD: "Good Website",
            WebsiteStatus.UNKNOWN: "Unknown",
        }[self]


class LeadStatus(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    RESPONDED = "responded"
    MEETING_BOOKED = "meeting_booked"
    PROPOSAL_SENT = "proposal_sent"
    WON = "won"
    LOST = "lost"
    DISQUALIFIED = "disqualified"
    DO_NOT_CONTACT = "do_not_contact"

    @property
    def is_open(self) -> bool:
        return self not in {
            LeadStatus.WON,
            LeadStatus.LOST,
            LeadStatus.DISQUALIFIED,
            LeadStatus.DO_NOT_CONTACT,
        }


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class AuditOutcome(StrEnum):
    OK = "ok"
    DNS_FAILURE = "dns_failure"
    CONNECTION_REFUSED = "connection_refused"
    TIMEOUT = "timeout"
    TLS_ERROR = "tls_error"
    HTTP_ERROR = "http_error"
    PARKED = "parked"
    BLOCKED = "blocked"
    SKIPPED_ROBOTS = "skipped_robots"
    ERROR = "error"


# =============================================================================
# Discovery
# =============================================================================


@dataclass(slots=True)
class SearchCell:
    """One provider query: a point, a radius, and what to look for there."""

    lat: float
    lng: float
    radius_m: int
    label: str
    city: str | None = None
    county: str | None = None
    state: str | None = None
    zip_code: str | None = None
    population: int | None = None

    def key(self) -> str:
        return f"{self.lat:.5f},{self.lng:.5f}@{self.radius_m}"


@dataclass(slots=True)
class RawBusiness:
    """A business exactly as a provider reported it — no scoring, no cleanup.

    ``source`` and ``source_id`` together identify the record upstream, which
    is what makes re-running a search idempotent.
    """

    source: str
    source_id: str
    name: str

    phone: str | None = None
    website: str | None = None
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    country: str = "US"
    lat: float | None = None
    lng: float | None = None

    google_maps_url: str | None = None
    categories: list[str] = field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    price_level: int | None = None
    hours: dict[str, str] = field(default_factory=dict)
    business_status: str | None = None
    description: str | None = None

    niche_key: str | None = None
    search_cell: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def full_address(self) -> str:
        parts = [self.street_address, self.city, self.state, self.zip_code]
        return ", ".join(p for p in parts if p)

    @property
    def is_operational(self) -> bool:
        if self.business_status is None:
            return True
        return self.business_status.upper() in {"OPERATIONAL", "OPEN"}


# =============================================================================
# Website evaluation
# =============================================================================


@dataclass(slots=True)
class Signal:
    """One observable fact about a website.

    ``value`` is the raw measurement, ``points`` the 0-100 contribution to its
    dimension, and ``note`` the sentence a human reads. Storing all three is
    what makes the score explainable instead of magic.
    """

    key: str
    dimension: str
    value: Any
    points: float
    weight: float = 1.0
    note: str = ""
    is_problem: bool = False


@dataclass(slots=True)
class WebsiteAudit:
    """Everything learned about one website in one pass."""

    url: str
    final_url: str | None = None
    outcome: AuditOutcome = AuditOutcome.OK
    error: str | None = None

    # transport
    http_status: int | None = None
    redirect_chain: list[str] = field(default_factory=list)
    response_time_ms: int | None = None
    uses_https: bool = False
    https_upgrade: bool = False
    ssl_valid: bool = False
    ssl_expires_at: datetime | None = None
    ssl_days_remaining: int | None = None
    mixed_content: bool = False
    security_headers: dict[str, str] = field(default_factory=dict)

    # content / seo
    title: str | None = None
    title_length: int = 0
    meta_description: str | None = None
    meta_description_length: int = 0
    h1_count: int = 0
    heading_outline_ok: bool = False
    canonical_url: str | None = None
    has_viewport_meta: bool = False
    has_favicon: bool = False
    has_open_graph: bool = False
    has_structured_data: bool = False
    has_sitemap: bool = False
    has_robots_txt: bool = False
    lang_attribute: str | None = None
    word_count: int = 0
    copyright_year: int | None = None

    # performance
    page_weight_kb: float | None = None
    request_count: int | None = None
    lcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None
    fcp_ms: float | None = None
    psi_performance: float | None = None
    psi_seo: float | None = None
    psi_accessibility: float | None = None
    psi_best_practices: float | None = None

    # mobile / rendering
    mobile_friendly: bool | None = None
    horizontal_overflow: bool = False
    smallest_font_px: float | None = None
    tap_target_issues: int = 0
    viewport_meta_content: str | None = None

    # design & conversion
    tech_stack: list[str] = field(default_factory=list)
    is_flash_or_legacy: bool = False
    uses_table_layout: bool = False
    has_contact_form: bool = False
    has_click_to_call: bool = False
    has_cta: bool = False
    has_social_links: bool = False
    image_count: int = 0
    broken_link_count: int = 0
    checked_link_count: int = 0
    images_missing_alt: int = 0
    total_images: int = 0
    design_era: str | None = None
    screenshot_path: str | None = None
    screenshot_mobile_path: str | None = None

    # discovered contacts
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)

    # scoring output
    signals: list[Signal] = field(default_factory=list)
    dimension_scores: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    status: WebsiteStatus = WebsiteStatus.UNKNOWN
    problems: list[str] = field(default_factory=list)

    checked_at: datetime = field(default_factory=datetime.utcnow)
    duration_ms: int = 0

    @property
    def reachable(self) -> bool:
        return self.outcome == AuditOutcome.OK

    def signals_for(self, dimension: str) -> list[Signal]:
        return [s for s in self.signals if s.dimension == dimension]


# =============================================================================
# Scoring
# =============================================================================


@dataclass(slots=True)
class ScoreComponent:
    key: str
    label: str
    raw: float          # 0-100 before weighting
    weight: float
    contribution: float  # raw * weight
    explanation: str


@dataclass(slots=True)
class ScoreResult:
    score: int
    components: list[ScoreComponent] = field(default_factory=list)
    adjustments: list[tuple[str, float, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    qualified: bool = False

    def explain(self) -> str:
        lines = [f"Lead score: {self.score}/100"]
        for c in sorted(self.components, key=lambda x: -x.contribution):
            lines.append(f"  {c.label:<20} {c.raw:5.1f} x {c.weight:.2f} = {c.contribution:5.1f}")
        for name, delta, why in self.adjustments:
            lines.append(f"  {name:<20} {delta:+6.1f}  ({why})")
        return "\n".join(lines)


# =============================================================================
# AI enrichment
# =============================================================================


@dataclass(slots=True)
class AIEnrichment:
    summary: str = ""
    opportunity: str = ""
    redesign_recommendations: list[str] = field(default_factory=list)
    outreach_angle: str = ""
    outreach_subject: str = ""
    talking_points: list[str] = field(default_factory=list)
    inferred_niche: str | None = None
    estimated_value_tier: str | None = None
    confidence: float = 0.0
    model: str = ""
    tokens_used: int = 0


# =============================================================================
# Pipeline results
# =============================================================================


@dataclass(slots=True)
class StageStats:
    name: str
    started_at: datetime
    finished_at: datetime | None = None
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class RunResult:
    run_id: int | None
    run_date: date
    status: RunStatus
    discovered: int = 0
    new_businesses: int = 0
    duplicates: int = 0
    audited: int = 0
    scored: int = 0
    qualified: int = 0
    enriched: int = 0
    errors: list[str] = field(default_factory=list)
    stages: list[StageStats] = field(default_factory=list)
    report_path: str | None = None
    api_calls: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None

    @property
    def duration_s(self) -> float:
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()
