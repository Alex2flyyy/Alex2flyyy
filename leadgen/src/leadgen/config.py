"""Runtime configuration.

Two layers, deliberately separate:

* :class:`Settings` — secrets and per-deployment knobs, from environment
  variables. Never checked into git.
* YAML catalogs in ``config/`` — niches, locations, scoring weights. These are
  business logic you want in version control and diffable in code review.

Both are loaded once and cached. Import :func:`get_settings`, :func:`get_niches`,
:func:`get_locations`, and :func:`get_scoring` rather than reading files
directly, so tests can override the cache in one place.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: src/leadgen/config.py -> src/leadgen -> src -> <root>
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


# =============================================================================
# Environment settings
# =============================================================================


class Settings(BaseSettings):
    """Environment-driven configuration.

    Everything is prefixed ``LEADGEN_`` except third-party conventions that
    already have a well-known name (``ANTHROPIC_API_KEY``).
    """

    model_config = SettingsConfigDict(
        env_prefix="LEADGEN_",
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core ---
    env: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    timezone: str = "America/Los_Angeles"
    config_dir: Path = DEFAULT_CONFIG_DIR

    # --- database ---
    database_url: str = "postgresql+asyncpg://leadgen:leadgen@localhost:5432/leadgen"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- discovery ---
    google_maps_api_key: str | None = None
    pagespeed_api_key: str | None = None
    serpapi_key: str | None = None
    bing_maps_key: str | None = None
    foursquare_api_key: str | None = None
    discovery_providers: str = "google_places,osm"

    # --- AI ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    ai_model: str = "claude-sonnet-5"
    ai_model_premium: str = "claude-opus-5"
    ai_enabled: bool = True
    ai_max_concurrency: int = 4
    ai_daily_token_budget: int = 2_000_000

    # --- pipeline ---
    daily_lead_target: int = 50
    daily_discovery_cap: int = 2000
    max_concurrent_audits: int = 8
    audit_timeout_seconds: int = 45
    enable_browser_audit: bool = True
    enable_screenshots: bool = True
    screenshot_dir: Path = Path("./data/screenshots")
    schedule_hour: int = 6

    # --- politeness ---
    user_agent: str = "leadgen-prospect-bot/1.0 (+https://example.com/bot)"
    respect_robots_txt: bool = True
    per_host_rps: float = 0.5
    global_rps: float = 12.0
    crawl_max_pages_per_site: int = 5

    # --- api ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str | None = None
    dashboard_enabled: bool = True
    cors_origins: str = ""

    # --- exports ---
    export_dir: Path = Path("./data/exports")
    google_sheets_credentials_file: Path | None = None
    google_sheets_spreadsheet_id: str | None = None
    notion_api_key: str | None = None
    notion_database_id: str | None = None
    airtable_api_key: str | None = None
    airtable_base_id: str | None = None
    airtable_table_name: str = "Leads"

    # --- notifications ---
    slack_webhook_url: str | None = None
    report_email_to: str | None = None

    # -- derived ----------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def provider_order(self) -> list[str]:
        return [p.strip() for p in self.discovery_providers.split(",") if p.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sync_database_url(self) -> str:
        """Alembic and other sync consumers need a non-async driver."""
        return self.database_url.replace("+asyncpg", "+psycopg").replace(
            "postgresql://", "postgresql+psycopg://"
        )

    @property
    def pagespeed_key(self) -> str | None:
        return self.pagespeed_api_key or self.google_maps_api_key

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {v}")
        return v

    @model_validator(mode="after")
    def _production_guardrails(self) -> Settings:
        # Failing fast at boot beats discovering at 3am that the nightly run has
        # been writing to a database nobody configured.
        if self.env == "production":
            missing: list[str] = []
            if not self.api_key:
                missing.append("LEADGEN_API_KEY")
            if "localhost" in self.database_url and not os.getenv("LEADGEN_ALLOW_LOCAL_DB"):
                missing.append("LEADGEN_DATABASE_URL (still pointing at localhost)")
            if missing:
                raise ValueError(
                    "production environment is missing required settings: "
                    + ", ".join(missing)
                )
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# =============================================================================
# YAML catalogs
# =============================================================================


class Niche(BaseModel):
    key: str
    label: str
    place_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    osm_tags: list[str] = Field(default_factory=list)
    demand: float = 0.7
    competition: float = 0.7
    ticket: float = 0.5
    web_expectation: str = ""

    @field_validator("demand", "competition", "ticket")
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"must be between 0 and 1, got {v}")
        return v


class NicheCatalog(BaseModel):
    niches: list[Niche]

    def get(self, key: str) -> Niche:
        for n in self.niches:
            if n.key == key:
                return n
        raise KeyError(
            f"unknown niche {key!r}. Known niches: {', '.join(n.key for n in self.niches)}"
        )

    @property
    def keys(self) -> list[str]:
        return [n.key for n in self.niches]


class LocationTarget(BaseModel):
    key: str = ""
    label: str
    kind: Literal["nation", "state", "county", "city", "zip", "radius"]

    country_code: str = "US"
    state: str | None = None
    county: str | None = None
    city: str | None = None
    zips: list[str] = Field(default_factory=list)
    center: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_miles: float | None = None

    cell_radius_m: int = 4000
    max_cells: int = 40
    min_population: int = 2000

    @model_validator(mode="after")
    def _require_fields_for_kind(self) -> LocationTarget:
        need: dict[str, tuple[str, ...]] = {
            "nation": ("country_code",),
            "state": ("state",),
            "county": ("county", "state"),
            "city": ("city", "state"),
            "zip": ("zips",),
        }
        for field in need.get(self.kind, ()):
            if not getattr(self, field):
                raise ValueError(f"location kind {self.kind!r} requires field {field!r}")
        if self.kind == "radius":
            if self.center is None and (self.lat is None or self.lng is None):
                raise ValueError("radius targets need either `center` or both `lat` and `lng`")
            if not self.radius_miles:
                raise ValueError("radius targets need `radius_miles`")
        return self


class LocationCatalog(BaseModel):
    defaults: dict[str, Any] = Field(default_factory=dict)
    targets: dict[str, LocationTarget]

    def get(self, key: str) -> LocationTarget:
        try:
            return self.targets[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown location {key!r}. Known targets: {', '.join(sorted(self.targets))}"
            ) from exc

    @property
    def keys(self) -> list[str]:
        return sorted(self.targets)


class WebsiteScoreConfig(BaseModel):
    weights: dict[str, float]
    good_at_or_above: int = 70
    poor_below: int = 50

    @field_validator("weights")
    @classmethod
    def _sums_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"website_score weights must sum to 1.0, got {total:.4f}")
        return v


class ActivityConfig(BaseModel):
    reviews_floor: int = 3
    reviews_strong: int = 40
    reviews_saturated: int = 300


class LeadScoreConfig(BaseModel):
    weights: dict[str, float]
    adjustments: dict[str, float] = Field(default_factory=dict)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
    qualification_threshold: int = 55

    @field_validator("weights")
    @classmethod
    def _sums_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"lead_score weights must sum to 1.0, got {total:.4f}")
        return v


class ChainSignals(BaseModel):
    name_patterns: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    @functools.cached_property
    def compiled_names(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self.name_patterns]


class ScoringConfig(BaseModel):
    website_score: WebsiteScoreConfig
    lead_score: LeadScoreConfig
    chain_signals: ChainSignals = Field(default_factory=ChainSignals)
    social_as_website_domains: list[str] = Field(default_factory=list)
    free_builder_domains: list[str] = Field(default_factory=list)

    model_config = {"ignored_types": (functools.cached_property,)}

    def adjustment(self, name: str) -> float:
        return float(self.lead_score.adjustments.get(name, 0.0))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found: {path}. Copy the defaults from the repo's "
            f"config/ directory or set LEADGEN_CONFIG_DIR."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


@functools.lru_cache(maxsize=1)
def get_niches() -> NicheCatalog:
    return NicheCatalog(**_load_yaml(get_settings().config_dir / "niches.yml"))


@functools.lru_cache(maxsize=1)
def get_locations() -> LocationCatalog:
    raw = _load_yaml(get_settings().config_dir / "locations.yml")
    defaults = raw.get("defaults") or {}
    targets: dict[str, LocationTarget] = {}
    for key, spec in (raw.get("targets") or {}).items():
        merged = {**defaults, **spec, "key": key}
        targets[key] = LocationTarget(**merged)
    return LocationCatalog(defaults=defaults, targets=targets)


@functools.lru_cache(maxsize=1)
def get_scoring() -> ScoringConfig:
    return ScoringConfig(**_load_yaml(get_settings().config_dir / "scoring.yml"))


def reset_config_cache() -> None:
    """Drop every cached config. Used by tests and by ``leadgen config reload``."""
    for fn in (get_settings, get_niches, get_locations, get_scoring):
        fn.cache_clear()
