# leadgen

An automated lead generation system for a website design business. Every day it
finds local businesses whose web presence is costing them customers, proves it
with measurable evidence, ranks them by how likely they are to buy, and hands
you a call list.

It is a working system, not a scaffold: 16 database tables, 8 evaluation
dimensions, 5 scoring components, 6 export destinations, and 167 tests that run
against a real PostgreSQL instance.

---

## What it actually does

```
leadgen run-daily --location home_base_25mi --target 50
```

1. **Plans.** Turns a location target ("25 miles around Pasadena", "all of
   California", "these 24 ZIP codes") into an ordered list of search cells.
2. **Discovers.** Queries Google Places, OpenStreetMap, and optionally SerpAPI
   for businesses in each niche, deduplicating as it goes.
3. **Audits.** Loads each website and measures it: HTTPS and certificate
   validity, mobile rendering in a real browser at 390px, Google PageSpeed and
   Core Web Vitals, SEO basics, design era, contact paths, broken links,
   accessibility.
4. **Enriches.** Finds published email addresses and contact forms on the
   business's own site.
5. **Scores.** Produces a website quality score (0-100, higher is better for
   them) and a lead score (0-100, higher means call them first), each with a
   plain-English explanation of every point.
6. **Writes it up.** Uses Claude to turn the findings into a business summary,
   a redesign opportunity, and an outreach angle — grounded only in what was
   actually observed.
7. **Delivers.** Generates the day's top 50 as HTML, CSV, and Excel, and can
   push to Google Sheets, Notion, Airtable, or a CRM-shaped file.

Everything is stored, so tomorrow's run knows what it saw yesterday: new
businesses are flagged, unchanged ones are skipped, and a site that breaks or
gets rebuilt is recorded as a status change.

---

## Quick start

```bash
cd leadgen
cp .env.example .env          # add LEADGEN_GOOGLE_MAPS_API_KEY and ANTHROPIC_API_KEY

docker compose up -d postgres # or point LEADGEN_DATABASE_URL at your own
pip install -e ".[dev]"
python -m playwright install --with-deps chromium

alembic upgrade head          # create the schema
leadgen geo seed              # load the starter gazetteer
leadgen config check          # verify what is and isn't wired up

leadgen run-daily --location pasadena --niche plumbing --target 25
leadgen serve                 # dashboard at http://localhost:8000
```

No Google key? It still runs — discovery falls back to OpenStreetMap and
geocoding to Nominatim. You get fewer businesses and no ratings, but the
audit, scoring, and reporting pipeline is identical.

Full instructions: [docs/INSTALL.md](docs/INSTALL.md).

---

## The two scores

Keeping these separate is the central design decision, and it is what makes the
output trustworthy.

**Website score** (0-100, higher is better *for the business*) is what you show
the prospect. It combines eight weighted dimensions — availability, security,
mobile, performance, SEO, design, conversion, accessibility — each built from
individual observations that carry their own human-readable note.

**Lead score** (0-100, higher is better *for you*) is what orders your call
list. It is not the inverse of the website score, because the worst website in
town might belong to a business that closed in 2019. It weighs:

| Component | Weight | What it captures |
|---|---|---|
| Web opportunity | 0.42 | How much room there is to improve |
| Revenue potential | 0.16 | Niche ticket size × apparent business scale |
| Reachability | 0.14 | Whether you can actually contact a decision maker |
| Business activity | 0.14 | Reviews, rating, hours, operating status |
| Market pressure | 0.14 | Local competition × local search demand |

Then explicit adjustments handle what a weighted average handles badly: a
franchise gets −22 because corporate owns the website; a business with no phone,
email, or form gets −18 because you cannot sell to someone you cannot reach; a
Facebook page standing in for a website gets +9 because that owner already
wants a web presence and settled.

Every score carries its full breakdown. On any lead detail page you can see
exactly which observations produced which points.

```
Lead score: 93/100
  Web opportunity       75.0 x 0.42 =  31.5
  Business activity     95.0 x 0.14 =  13.3
  Reachability          90.0 x 0.14 =  12.6
  Revenue potential     77.5 x 0.16 =  12.4
  Market pressure       87.8 x 0.14 =  12.3
  ssl_missing_bonus      +6.0  (no valid SSL certificate)
  high_rating_many_reviews_bonus   +5.0  (thriving business that can afford a redesign)
```

Weights live in `config/scoring.yml`. Change them and rerun; nothing is
hard-coded.

---

## Configuration

Three YAML files hold the business logic, so tuning does not require touching
Python.

**`config/niches.yml`** — 37 niches. Each maps a label onto Google Place types,
search keywords, OSM tags, and three economic dials (`demand`, `competition`,
`ticket`) that feed the lead score. Add a niche by appending a block.

**`config/locations.yml`** — named geographic targets in six kinds: `nation`,
`state`, `county`, `city`, `zip`, and `radius`. Large scopes are seeded from
population centers rather than blind-tiled, which is what makes a
"whole United States" target affordable instead of ~250,000 API calls.

**`config/scoring.yml`** — all scoring weights, thresholds, and the chain and
site-builder detection lists.

Preview what a target will actually search, and what it will cost, before
spending anything:

```bash
leadgen geo plan california --niche roofing
```

---

## Commands

```
leadgen run-daily          Run the full pipeline
leadgen scheduler          Run forever, firing once a day
leadgen report             Regenerate today's report from stored leads
leadgen export <dest>      csv | xlsx | google_sheets | notion | airtable | crm
leadgen audit <url>        Audit one website and print findings (no database)
leadgen serve              Start the API and dashboard
leadgen stats              Print current pipeline statistics
leadgen suppress <value>   Add to the do-not-contact list
leadgen geo seed|import|plan
leadgen config check|show|niches|locations
```

`leadgen audit` needs nothing but a URL and is the fastest way to see what the
system measures:

```bash
leadgen audit https://example-plumber.com
```

---

## Dashboard and API

`leadgen serve` starts both on port 8000.

The dashboard is server-rendered Jinja with a little vanilla JavaScript — no
build step, no Node toolchain. It shows today's leads, totals by city and
niche, website-status distribution, the conversion funnel, and a per-lead detail
page with the full score breakdown, audit findings, mobile screenshot, and AI
talking points.

The JSON API is documented at `/docs`. Read endpoints are open; write endpoints
require an `X-API-Key` header.

```
GET   /api/leads                  search and filter
GET   /api/leads/today            today's top prospects
GET   /api/leads/{id}             full detail with audit and insight
PATCH /api/leads/{id}             update pipeline status and notes
POST  /api/leads/{id}/suppress    do-not-contact
GET   /api/stats                  everything the dashboard needs
GET   /api/stats/website-changes  sites that recently broke or improved
POST  /api/runs/trigger           start a run in the background
POST  /api/exports                run an export
```

---

## Scaling

The default run targets 50 qualified leads. The knobs that move it:

| Setting | Effect |
|---|---|
| `LEADGEN_DAILY_DISCOVERY_CAP` | Ceiling on businesses fetched per run |
| `max_cells` in a location target | How much geography gets searched |
| `cell_radius_m` | Smaller = denser coverage, more calls |
| `LEADGEN_MAX_CONCURRENT_AUDITS` | Audit throughput |
| `LEADGEN_ENABLE_BROWSER_AUDIT=false` | ~5× faster audits, loses mobile checks |
| `--no-pagespeed` | Removes the slowest step (10-30s per site) |

To reach 500/day, widen to a county target and raise the cap. To reach
5,000/day, use a state target, disable PageSpeed for the bulk pass, and raise
audit concurrency — then re-audit only the top-scoring leads with the full
suite. The bottleneck at every level is the audit stage, not the database.

Real constraints to respect while scaling: provider quotas and terms,
`robots.txt`, per-host rate limits (a small business site is often on shared
hosting — hammering it is a denial of service, not a crawl), and the marketing
laws covered in [docs/COMPLIANCE.md](docs/COMPLIANCE.md).

---

## Technology choices

| Choice | Why |
|---|---|
| **Python 3.11** | Best ecosystem for scraping, data, and AI. `asyncio` fits a workload that is almost entirely network I/O. |
| **PostgreSQL** | JSONB for evolving audit payloads, array columns, partial indexes, and `INSERT ... ON CONFLICT` — which is what makes concurrent deduplication correct rather than hopeful. |
| **SQLAlchemy 2.0 + Alembic** | Typed ORM with real async support; versioned, reversible schema changes. CI fails if models and migrations drift. |
| **httpx** | Async HTTP with HTTP/2 and connection pooling. One shared, rate-limited client for all outbound traffic. |
| **selectolax** | C-backed HTML parser, roughly 20× faster than BeautifulSoup. At 5,000 sites a night that is minutes instead of an hour. |
| **Playwright** | The only way to answer "does this actually break on a phone?" Comparing `scrollWidth` to `clientWidth` at 390px is proof; a viewport meta tag is just a claim. Also produces the screenshots that sell the redesign. |
| **PageSpeed Insights API** | A performance number *from Google* ends the argument in a way your own stopwatch never will. Free, and it is the same signal that feeds Google's ranking. |
| **Google Places API (New)** | Licensed, structured, and will not get your IP blocked mid-run — unlike scraping Maps. |
| **OpenStreetMap / Overpass** | Free, and biased toward exactly the businesses you want: OSM entries frequently lack a website tag because the business genuinely has none. |
| **Claude API** | Turns observations into a pitch. Tool-use schemas force structured output, so responses are validated data instead of prose to regex. Strictly optional — the pipeline's core value does not depend on it. |
| **FastAPI** | Async-native, generates the OpenAPI docs, and Pydantic validation at the boundary. |
| **Jinja + vanilla JS** | This dashboard serves one to three people and must work at 6am. A Node build would be more maintenance surface than the dashboard itself. |
| **Typer + structlog + Rich** | One CLI path shared by cron, Docker, and humans; JSON logs in production that a log shipper can index by `run_id`. |
| **Docker Compose** | Postgres, migrations, API, and scheduler in one reproducible stack. |
| **GitHub Actions** | Zero-infrastructure scheduling, with run history and report artifacts kept for you. |

---

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component map, data flow, database schema, extension points |
| [docs/INSTALL.md](docs/INSTALL.md) | Installation, API keys, environment variables |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, GitHub Actions, VPS, security hardening |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Daily running, tuning, troubleshooting, maintenance |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | robots.txt, CAN-SPAM, TCPA, CCPA, provider terms |

---

## Testing

```bash
pytest                                    # pure logic only
TEST_DATABASE_URL=postgresql+asyncpg://... pytest   # plus database and API
```

167 tests. Database tests run against real PostgreSQL because the schema
depends on Postgres-specific behavior — testing the deduper against a database
that does not enforce its unique constraints would test nothing. Without
`TEST_DATABASE_URL` those tests skip and the rest still run.

The tests assert behavior rather than magic numbers: a bad site must score below
a good one, an unreachable business must not outrank a reachable one, hex tiling
must leave no uncovered gaps, and re-scoring a lead must never destroy the notes
you wrote after calling them.

---

## Status and honest limits

Verified working: schema and migrations against PostgreSQL 16, deduplication
under real unique constraints, both scorers, HTML analysis across four site
archetypes, hex-tile coverage, report generation in three formats, all CLI
commands, and every API endpoint and dashboard page.

Not verified here, because it requires paid credentials: live Google Places
discovery, live PageSpeed results, and live Claude enrichment. Those code paths
have unit coverage and defined failure behavior, but run `leadgen config check`
and a small `--dry-run` before trusting a scheduled job.

The bundled gazetteer holds 254 real US places, weighted toward California.
That is enough for city, county, and radius targets. Genuine nationwide
coverage needs a full gazetteer — see `leadgen geo import --help`.

Before running outreach at volume, read
[docs/COMPLIANCE.md](docs/COMPLIANCE.md) and talk to a lawyer. Collecting
publicly listed business information for B2B outreach is lawful in the United
States; it is not unconditional.
