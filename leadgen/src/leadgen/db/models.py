"""ORM models.

Schema shape, and why:

* ``businesses`` is the canonical entity. Everything else hangs off it.
  Deduplication is enforced *in the database* by two unique indexes
  (``source_key`` and ``dedupe_key``) rather than trusting application logic —
  a concurrent run cannot create a twin.
* ``leads`` is 1:1 with a business but separate, because a business is a fact
  about the world and a lead is a fact about your sales process. Mixing them
  means re-running discovery clobbers your pipeline notes.
* ``website_audits`` is append-only. Keeping history is what lets the dashboard
  say "this site got worse in March" and what powers re-engagement triggers.
* ``lead_score_history`` likewise: score movement over time is a selling signal.

Enums are stored as VARCHAR with a CHECK constraint (``native_enum=False``)
instead of Postgres ENUM types. Adding a value to a native ENUM requires a
migration with ``ALTER TYPE``, which cannot run inside a transaction on older
Postgres; VARCHAR + CHECK is boring and cheap to evolve.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leadgen.db.base import Base, TimestampMixin
from leadgen.domain import AuditOutcome, LeadStatus, RunStatus, WebsiteStatus


def _enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda e: [m.value for m in e],
        length=40,
    )


# =============================================================================
# Businesses
# =============================================================================


class Business(Base, TimestampMixin):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # --- identity / dedupe ---
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_key: Mapped[str] = mapped_column(String(300), nullable=False)
    # sha1(normalized name + normalized street + zip). Catches the same shop
    # arriving from a second provider under a different id.
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- core fields ---
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)

    # --- contact ---
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_e164: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)

    # --- location ---
    street_address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    county: Mapped[str | None] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(2), index=True)
    zip_code: Mapped[str | None] = mapped_column(String(10), index=True)
    country: Mapped[str] = mapped_column(String(2), default="US", nullable=False)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))

    # --- listing data ---
    google_place_id: Mapped[str | None] = mapped_column(String(255), index=True)
    google_maps_url: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(Text)
    website_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    niche_key: Mapped[str | None] = mapped_column(String(60), index=True)
    categories: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)
    rating: Mapped[float | None] = mapped_column(Float)
    review_count: Mapped[int | None] = mapped_column(Integer)
    price_level: Mapped[int | None] = mapped_column(Integer)
    hours: Mapped[dict] = mapped_column(JSONB, default=dict)
    social_links: Mapped[dict] = mapped_column(JSONB, default=dict)
    business_status: Mapped[str | None] = mapped_column(String(40))
    is_chain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- provenance ---
    first_discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovery_location: Mapped[str | None] = mapped_column(String(120))
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    # --- relationships ---
    lead: Mapped[Lead | None] = relationship(
        back_populates="business", uselist=False, cascade="all, delete-orphan"
    )
    audits: Mapped[list[WebsiteAuditRecord]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        order_by="WebsiteAuditRecord.checked_at.desc()",
    )
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    insights: Mapped[list[AIInsight]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        order_by="AIInsight.created_at.desc()",
    )
    status_changes: Mapped[list[WebsiteStatusChange]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source_key", name="uq_businesses_source_key"),
        UniqueConstraint("dedupe_key", name="uq_businesses_dedupe_key"),
        Index("ix_businesses_city_state", "city", "state"),
        Index("ix_businesses_niche_state", "niche_key", "state"),
        # Partial index: "who has no website" is the single most common filter
        # in this system, and it is a tiny slice of the table.
        Index(
            "ix_businesses_no_website",
            "niche_key",
            "city",
            postgresql_where=(website_url.is_(None)),
        ),
        CheckConstraint("rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating_range"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Business {self.id} {self.name!r} {self.city}, {self.state}>"


# =============================================================================
# Website audits
# =============================================================================


class WebsiteAuditRecord(Base, TimestampMixin):
    """One website evaluation. Append-only history."""

    __tablename__ = "website_audits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )

    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[AuditOutcome] = mapped_column(
        _enum(AuditOutcome, "audit_outcome"), nullable=False, default=AuditOutcome.OK
    )
    error: Mapped[str | None] = mapped_column(Text)

    http_status: Mapped[int | None] = mapped_column(Integer)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    uses_https: Mapped[bool] = mapped_column(Boolean, default=False)
    ssl_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    ssl_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mixed_content: Mapped[bool] = mapped_column(Boolean, default=False)

    mobile_friendly: Mapped[bool | None] = mapped_column(Boolean)
    has_viewport_meta: Mapped[bool] = mapped_column(Boolean, default=False)
    horizontal_overflow: Mapped[bool] = mapped_column(Boolean, default=False)

    psi_performance: Mapped[float | None] = mapped_column(Float)
    psi_seo: Mapped[float | None] = mapped_column(Float)
    psi_accessibility: Mapped[float | None] = mapped_column(Float)
    psi_best_practices: Mapped[float | None] = mapped_column(Float)
    lcp_ms: Mapped[float | None] = mapped_column(Float)
    cls: Mapped[float | None] = mapped_column(Float)
    tbt_ms: Mapped[float | None] = mapped_column(Float)
    page_weight_kb: Mapped[float | None] = mapped_column(Float)

    title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    h1_count: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    copyright_year: Mapped[int | None] = mapped_column(Integer)
    has_structured_data: Mapped[bool] = mapped_column(Boolean, default=False)
    has_sitemap: Mapped[bool] = mapped_column(Boolean, default=False)

    has_contact_form: Mapped[bool] = mapped_column(Boolean, default=False)
    has_click_to_call: Mapped[bool] = mapped_column(Boolean, default=False)
    has_cta: Mapped[bool] = mapped_column(Boolean, default=False)
    broken_link_count: Mapped[int] = mapped_column(Integer, default=0)
    images_missing_alt: Mapped[int] = mapped_column(Integer, default=0)

    tech_stack: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    # Not a code — a sentence shown to the operator and used verbatim in the
    # pitch. It accretes clauses ("pre-2012 (not responsive), copyright 2011"
    # is already 41), so it needs room to grow.
    design_era: Mapped[str | None] = mapped_column(String(120))
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    screenshot_mobile_path: Mapped[str | None] = mapped_column(Text)

    dimension_scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    signals: Mapped[list] = mapped_column(JSONB, default=list)
    problems: Mapped[list] = mapped_column(JSONB, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[WebsiteStatus] = mapped_column(
        _enum(WebsiteStatus, "website_status"), nullable=False, default=WebsiteStatus.UNKNOWN
    )

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    business: Mapped[Business] = relationship(back_populates="audits")

    __table_args__ = (
        Index("ix_website_audits_business_checked", "business_id", "checked_at"),
        Index("ix_website_audits_status", "status"),
    )


class WebsiteStatusChange(Base, TimestampMixin):
    """Recorded when a business's website status transitions.

    Two uses: (1) a prospect whose site just broke is worth calling today,
    (2) a prospect whose site got *better* was probably sold by someone else,
    so stop pitching them.
    """

    __tablename__ = "website_status_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_status: Mapped[WebsiteStatus | None] = mapped_column(_enum(WebsiteStatus, "website_status"))
    new_status: Mapped[WebsiteStatus] = mapped_column(
        _enum(WebsiteStatus, "website_status"), nullable=False
    )
    old_score: Mapped[float | None] = mapped_column(Float)
    new_score: Mapped[float | None] = mapped_column(Float)
    old_url: Mapped[str | None] = mapped_column(Text)
    new_url: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    business: Mapped[Business] = relationship(back_populates="status_changes")


# =============================================================================
# Leads
# =============================================================================


class Lead(Base, TimestampMixin):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    previous_score: Mapped[int | None] = mapped_column(Integer)
    website_status: Mapped[WebsiteStatus] = mapped_column(
        _enum(WebsiteStatus, "website_status"), default=WebsiteStatus.UNKNOWN, nullable=False
    )
    website_score: Mapped[float | None] = mapped_column(Float)
    qualified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    status: Mapped[LeadStatus] = mapped_column(
        _enum(LeadStatus, "lead_status"), default=LeadStatus.NEW, nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 highest .. 5 lowest

    # Human-readable justification, always populated. Never ship a bare number.
    reason: Mapped[str | None] = mapped_column(Text)
    score_components: Mapped[list] = mapped_column(JSONB, default=list)
    score_adjustments: Mapped[list] = mapped_column(JSONB, default=list)

    # Sales workflow
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_contact_channel: Mapped[str | None] = mapped_column(String(40))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    follow_up_status: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(120))

    # Outcome
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(String(200))
    deal_value: Mapped[float | None] = mapped_column(Numeric(12, 2))

    first_scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    business: Mapped[Business] = relationship(back_populates="lead")
    score_history: Mapped[list[LeadScoreHistory]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    activities: Mapped[list[OutreachActivity]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="OutreachActivity.occurred_at"
    )

    __table_args__ = (
        Index("ix_leads_qualified_score", "qualified", "score"),
        Index("ix_leads_status_score", "status", "score"),
        CheckConstraint("score >= 0 AND score <= 100", name="score_range"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),
    )


class LeadScoreHistory(Base):
    __tablename__ = "lead_score_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    website_score: Mapped[float | None] = mapped_column(Float)
    website_status: Mapped[WebsiteStatus | None] = mapped_column(
        _enum(WebsiteStatus, "website_status")
    )
    components: Mapped[list] = mapped_column(JSONB, default=list)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    lead: Mapped[Lead] = relationship(back_populates="score_history")


# =============================================================================
# Contacts
# =============================================================================


class Contact(Base, TimestampMixin):
    """A discovered way to reach a business.

    Split from ``businesses`` because one business routinely has several
    (info@, the owner's cell, a contact form URL) and you want per-channel
    confidence and verification state.
    """

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # email|phone|form|social
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    label: Mapped[str | None] = mapped_column(String(80))
    person_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(60), default="website")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_generic: Mapped[bool] = mapped_column(Boolean, default=False)  # info@, contact@
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship(back_populates="contacts")

    __table_args__ = (
        UniqueConstraint("business_id", "kind", "value", name="uq_contacts_business_kind_value"),
    )


# =============================================================================
# AI insights
# =============================================================================


class AIInsight(Base, TimestampMixin):
    __tablename__ = "ai_insights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )

    summary: Mapped[str | None] = mapped_column(Text)
    opportunity: Mapped[str | None] = mapped_column(Text)
    redesign_recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    outreach_angle: Mapped[str | None] = mapped_column(Text)
    outreach_subject: Mapped[str | None] = mapped_column(String(300))
    talking_points: Mapped[list] = mapped_column(JSONB, default=list)
    inferred_niche: Mapped[str | None] = mapped_column(String(60))
    estimated_value_tier: Mapped[str | None] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    model: Mapped[str | None] = mapped_column(String(60))
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    # Hash of the inputs the summary was generated from. If nothing material
    # changed, we skip the API call on the next run — this is the single
    # biggest AI cost saver in the system.
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    business: Mapped[Business] = relationship(back_populates="insights")


# =============================================================================
# Pipeline runs
# =============================================================================


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="scheduled")  # scheduled|manual|api
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), default=RunStatus.PENDING, nullable=False, index=True
    )

    location_key: Mapped[str | None] = mapped_column(String(80))
    niche_keys: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)
    target_leads: Mapped[int] = mapped_column(Integer, default=50)

    discovered: Mapped[int] = mapped_column(Integer, default=0)
    new_businesses: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    audited: Mapped[int] = mapped_column(Integer, default=0)
    scored: Mapped[int] = mapped_column(Integer, default=0)
    qualified: Mapped[int] = mapped_column(Integer, default=0)
    enriched: Mapped[int] = mapped_column(Integer, default=0)

    api_calls: Mapped[dict] = mapped_column(JSONB, default=dict)
    stages: Mapped[list] = mapped_column(JSONB, default=list)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (Index("ix_pipeline_runs_date_status", "run_date", "status"),)


class DailyReport(Base, TimestampMixin):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    lead_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    lead_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    html_path: Mapped[str | None] = mapped_column(Text)
    csv_path: Mapped[str | None] = mapped_column(Text)
    excel_path: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("report_date", name="uq_daily_reports_report_date"),)


# =============================================================================
# Outreach + suppression (future-facing, wired now so migrations stay additive)
# =============================================================================


class OutreachActivity(Base, TimestampMixin):
    __tablename__ = "outreach_activities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)  # email|call|sms|dm|mail
    direction: Mapped[str] = mapped_column(String(10), default="outbound")
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(60))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    lead: Mapped[Lead] = relationship(back_populates="activities")


class Suppression(Base, TimestampMixin):
    """Do-not-contact list.

    Checked before a lead is ever surfaced in a report or export. Honoring an
    opt-out is a legal obligation under CAN-SPAM / TCPA / CCPA, not a nicety,
    so it lives in the database with its own table rather than as a lead flag
    that a re-score could overwrite.
    """

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # email|phone|domain|business
    value: Mapped[str] = mapped_column(String(400), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(60), default="manual")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppressions_kind_value"),)


class ExportJob(Base, TimestampMixin):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    destination: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    output_path: Mapped[str | None] = mapped_column(Text)
    remote_ref: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# =============================================================================
# Geography
# =============================================================================


class GeoPlace(Base):
    """Gazetteer of US populated places.

    Seeded with a small bundled file; expand to full national coverage with
    ``leadgen geo import <csv>``. Population drives search-cell ordering, which
    is how a "whole United States" run stays affordable — you search Fresno
    before you search an unincorporated crossroads.
    """

    __tablename__ = "geo_places"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    county: Mapped[str | None] = mapped_column(String(120), index=True)
    lat: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    lng: Mapped[float] = mapped_column(Numeric(9, 6), nullable=False)
    population: Mapped[int] = mapped_column(Integer, default=0, index=True)
    zips: Mapped[list[str]] = mapped_column(ARRAY(String(10)), default=list)
    country_code: Mapped[str] = mapped_column(String(2), default="US", nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "normalized_name", "state", "country_code", name="uq_geo_places_name_state"
        ),
        Index("ix_geo_places_state_population", "state", "population"),
        Index("ix_geo_places_county_state", "county", "state"),
    )


class GeocodeCache(Base, TimestampMixin):
    """Cache for geocoder lookups.

    Geocoding the same twelve city names every night is pure waste and counts
    against a billed quota, so results are cached indefinitely — city
    centroids do not move.
    """

    __tablename__ = "geocode_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    query: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    viewport: Mapped[dict] = mapped_column(JSONB, default=dict)
    formatted_address: Mapped[str | None] = mapped_column(Text)
    components: Mapped[dict] = mapped_column(JSONB, default=dict)
    provider: Mapped[str] = mapped_column(String(40), default="google")
    found: Mapped[bool] = mapped_column(Boolean, default=True)


class RobotsCache(Base, TimestampMixin):
    """Cached robots.txt bodies, so a crawl doesn't refetch per page."""

    __tablename__ = "robots_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    body: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer)
