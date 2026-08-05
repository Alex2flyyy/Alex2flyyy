# Architecture

## Shape of the system

Six layers. Each depends only on the ones above it, which is what lets you swap
a data provider or a database without touching scoring logic.

```
┌──────────────────────────────────────────────────────────────────┐
│  INTERFACES        cli.py · api/ · web/templates                 │
├──────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION     pipeline/daily.py · pipeline/stages.py        │
├──────────────────────────────────────────────────────────────────┤
│  CAPABILITIES      discovery/ · evaluation/ · scoring/ · ai/      │
│                    enrichment/ · dedupe/ · geo/ · exports/        │
├──────────────────────────────────────────────────────────────────┤
│  DOMAIN            domain.py   (dataclasses, no I/O)             │
├──────────────────────────────────────────────────────────────────┤
│  PERSISTENCE       db/models.py · db/repositories.py             │
├──────────────────────────────────────────────────────────────────┤
│  INFRASTRUCTURE    http.py · ratelimit.py · logging.py ·          │
│                    config.py · compliance/                        │
└──────────────────────────────────────────────────────────────────┘
```

The pivot point is `domain.py`. A discovery provider returns a `RawBusiness`;
the auditor returns a `WebsiteAudit`; the scorer returns a `ScoreResult`. None
of those know about SQLAlchemy, and none know about HTTP responses. Only
`db/` speaks to the database and only `api/schemas.py` defines the wire format.

## Folder structure

```
leadgen/
├── config/                     Business logic as data
│   ├── niches.yml              37 niches with economic weights
│   ├── locations.yml           Named geographic targets
│   ├── scoring.yml             All weights, thresholds, detection lists
│   └── seed_places.csv         Starter gazetteer (254 real US places)
│
├── migrations/                 Alembic; versions/ holds the schema history
│
├── src/leadgen/
│   ├── config.py               Settings (env) + YAML catalogs, both cached
│   ├── domain.py               Transport dataclasses and enums
│   ├── http.py                 The one outbound HTTP client
│   ├── ratelimit.py            Token buckets, concurrency, quota counting
│   ├── logging.py              structlog: console in dev, JSON in prod
│   ├── cli.py                  Typer CLI — every operation is reachable here
│   │
│   ├── db/
│   │   ├── base.py             Declarative base, naming conventions
│   │   ├── models.py           16 tables
│   │   ├── repositories.py     All SQL lives here, nowhere else
│   │   └── session.py          Async engine and unit-of-work scope
│   │
│   ├── geo/
│   │   ├── grid.py             Haversine, hex tiling, coverage math
│   │   ├── geocoder.py         Google + Nominatim, permanently cached
│   │   └── resolver.py         Location target → ordered search cells
│   │
│   ├── discovery/
│   │   ├── base.py             Provider contract
│   │   ├── google_places.py    Primary source (Places API New)
│   │   ├── openstreetmap.py    Free fallback via Overpass
│   │   ├── serpapi.py          Optional; adds Maps rank as a signal
│   │   └── registry.py         Multi-provider orchestration
│   │
│   ├── evaluation/
│   │   ├── parser.py           Static HTML analysis, design-era heuristics
│   │   ├── http_probe          (in http.py) reachability + TLS inspection
│   │   ├── browser.py          Playwright render audit and screenshots
│   │   ├── pagespeed.py        Google Lighthouse via the PSI API
│   │   ├── links.py            Broken-link sampling
│   │   └── auditor.py          Tiered orchestration of all of the above
│   │
│   ├── scoring/
│   │   ├── website_score.py    8 dimensions → 0-100 + status + problems
│   │   └── lead_score.py       5 components + adjustments → 0-100 + reasons
│   │
│   ├── ai/
│   │   ├── client.py           Anthropic wrapper: budget, retries, tool use
│   │   ├── prompts.py          System prompt and tool schemas
│   │   └── enrichment.py       Enrichment service with input hashing
│   │
│   ├── enrichment/
│   │   ├── normalize.py        Names, phones, addresses, domains, emails
│   │   └── contact.py          Contact discovery on the business's own site
│   │
│   ├── dedupe/matcher.py       Three-tier deduplication
│   ├── compliance/             robots.txt handling and marketing-law policy
│   ├── pipeline/               daily.py orchestrator, stages.py persistence
│   ├── exports/                CSV, Excel, Sheets, Notion, Airtable, CRM
│   ├── reports/                Daily report builder + HTML template
│   ├── api/                    FastAPI app, routers, schemas, dependencies
│   └── web/templates/          Dashboard (Jinja)
│
├── tests/                      167 tests
└── docs/
```

## Data flow

```
LocationTarget ──resolver──► [SearchCell]  (ordered by population)
                                  │
                                  ▼
                        DiscoveryOrchestrator
                     google_places → osm → serpapi
                                  │
                                  ▼  [RawBusiness]
                          InMemoryDeduper           ← drops within-run repeats
                                  │
                                  ▼
                        BusinessRepository.upsert    ← database enforces the rest
                                  │
                                  ▼  business_ids
                          WebsiteAuditor
              ┌───────────────────┼───────────────────┐
         HTTP + TLS         static HTML          Playwright render
              └───────────────────┼───────────────────┘
                        PageSpeed · broken links
                                  │
                                  ▼  WebsiteAudit
                          score_website()            → 0-100, status, problems
                                  │
                                  ▼
                          ContactEnricher            → emails, forms, socials
                                  │
                                  ▼
                          score_lead()               → 0-100, reasons, qualified
                                  │
                                  ▼
                      EnrichmentService (top N only) → summary, angle, points
                                  │
                                  ▼
                     Report → HTML · CSV · XLSX · Sheets · Notion · Airtable · CRM
```

## Database schema

16 tables. The load-bearing decisions:

**`businesses`** is the canonical entity, with two unique indexes that make
duplicates impossible rather than merely unlikely:

- `source_key` = `"{source}:{source_id}"` — the same provider record twice
- `dedupe_key` = `sha1(normalized_name | normalized_street | zip5)` — the same
  shop arriving from a second provider

For businesses with no street address (mobile detailers, service-area
contractors), `dedupe_key` falls back to phone instead of street. Without that,
every mobile business in a ZIP code would collapse into one row.

A partial index covers the system's single most common query:

```sql
CREATE INDEX ix_businesses_no_website ON businesses (niche_key, city)
  WHERE website_url IS NULL;
```

**`leads`** is 1:1 with a business but a separate table. A business is a fact
about the world; a lead is a fact about your sales process. Merging them means
tonight's discovery run overwrites the notes you wrote after calling someone.
`upsert_score()` deliberately writes only scoring columns and never touches
`status`, `notes`, `contacted_at`, or `follow_up_at`.

**`website_audits`** is append-only. History is what lets the dashboard say
"this site broke last Tuesday" and what powers re-engagement triggers.
**`website_status_changes`** records transitions, written only when something
actually moved.

**`lead_score_history`** does the same for scores: movement over time is itself
a selling signal.

**`suppressions`** is its own table rather than a flag on `leads`, because a
re-score must never be able to resurrect an opt-out. It is consulted at every
export and report boundary.

**`geo_places`** is the gazetteer that makes large scopes affordable.
**`geocode_cache`** and **`robots_cache`** avoid re-paying for answers that do
not change.

Supporting tables: `contacts`, `ai_insights`, `pipeline_runs`, `daily_reports`,
`outreach_activities`, `export_jobs`.

Enums are stored as `VARCHAR` with a `CHECK` constraint rather than native
Postgres `ENUM` types. Adding a value to a native enum needs `ALTER TYPE`, which
cannot run inside a transaction on older Postgres; varchar plus check is boring
and cheap to evolve.

### Relationships

```
businesses ─1:1─ leads ─1:N─ lead_score_history
     │                └─1:N─ outreach_activities
     ├─1:N─ website_audits
     ├─1:N─ website_status_changes
     ├─1:N─ contacts
     └─1:N─ ai_insights

pipeline_runs ─1:N─ website_audits · ai_insights · lead_score_history
daily_reports ─N:1─ pipeline_runs
```

## The audit's cost tiers

Tiering is what makes thousands of audits a night feasible. Each tier runs only
when the cheaper ones left the answer open.

| Tier | Cost | Runs when |
|---|---|---|
| 1. Reachability + TLS | 1 request | Always |
| 2. Static HTML | 0 extra | Always (site loaded) |
| 3. Browser render | ~1 page, 2-5s | HTML was near-empty (JS-rendered), or screenshots wanted, or no viewport meta, or a known SPA/builder stack |
| 4. PageSpeed | 10-30s | Enabled and the site is a real prospect |
| 5. Links + sitemap | ~20 HEADs | Enabled and internal links exist |

If tier 1 finds the domain does not resolve, nothing else runs — the outcome is
already the finding, and it is a strong one.

## Scoring architecture

Both scorers emit explanations alongside numbers, because an unexplained score
is one a salesperson stops trusting after the first bad call.

`score_website()` builds `Signal` objects — each with a key, dimension, raw
value, points, weight, and a human sentence — then aggregates them per
dimension and combines by config weight. A dimension with **no** observations
is dropped and its weight redistributed, rather than scored zero: penalizing a
site because PageSpeed timed out would put a fabricated problem in a sales
email.

`score_lead()` produces five weighted `ScoreComponent`s, then applies explicit
adjustments for situations a weighted average handles badly, then clamps to
0-100.

## Extension points

**A new data source** — implement `Provider` (two methods: `available()` and
`search()`), add one line to `PROVIDER_CLASSES` in `discovery/registry.py`.
Nothing downstream changes.

**A new audit signal** — append a `Signal` in the relevant builder in
`scoring/website_score.py`. Weights renormalize automatically.

**A new niche** — append a block to `config/niches.yml`. No code.

**A new export target** — add a function in `exports/`, register it in
`DESTINATIONS`. Every exporter consumes the same row shape from
`reports/daily_report.py:lead_to_row`, so a new field appears everywhere at
once.

**Planned features the schema already accommodates:** `outreach_activities`
exists for cold email and call logging; `deal_value` and `won_at` on `leads`
support revenue tracking; `export_jobs` supports async export queues; the
`ai_insights` table has room for generated mockups and proposals.

## Error handling

The governing rule: **a partial run that produces 38 leads beats a clean
exception at 6:04am.**

| Failure | Behavior |
|---|---|
| One provider out of quota | Disabled for the run, others continue |
| One business fails to parse | Logged and skipped, batch continues |
| Playwright not installed | Falls back to HTTP-only audits with a warning |
| PageSpeed times out | That dimension is dropped, not scored zero |
| AI budget exhausted | Report ships without summaries |
| A stage raises | Recorded on the run, status becomes `partial` |
| Database conflict on upsert | Counted as a duplicate — the constraint working |

Transaction boundaries are per stage with batched commits. One transaction
spanning a two-hour run would hold locks, bloat the WAL, and lose everything on
a failure at minute 118.

## Logging

structlog throughout: human-readable in development, JSON in production so a
shipper can index by field. Every run binds `run_id` once and every subsequent
line inherits it, which is what makes a failed nightly run debuggable
afterwards. Event names are dotted and stable (`discover.complete`,
`audit.batch`, `provider.quota`) so they can be alerted on.
