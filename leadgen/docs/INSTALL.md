# Installation

## Requirements

- Python 3.11+
- PostgreSQL 14+ (16 recommended)
- ~2 GB disk for Chromium, plus room for screenshots

## 1. Install

```bash
git clone <your-repo-url>
cd Alex2flyyy/leadgen

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install --with-deps chromium
```

`--with-deps` installs the system libraries Chromium needs. On a machine where
you cannot install system packages, use the Docker path in
[DEPLOYMENT.md](DEPLOYMENT.md) instead — the Playwright base image ships them.

## 2. Database

Either use the bundled Postgres:

```bash
docker compose up -d postgres
```

or point at your own and create the database:

```sql
CREATE DATABASE leadgen;
CREATE USER leadgen WITH PASSWORD 'change-me';
GRANT ALL PRIVILEGES ON DATABASE leadgen TO leadgen;
```

## 3. Configure

```bash
cp .env.example .env
```

Minimum viable configuration:

```bash
LEADGEN_DATABASE_URL=postgresql+asyncpg://leadgen:change-me@localhost:5432/leadgen
LEADGEN_GOOGLE_MAPS_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
```

The system runs with **none** of the API keys — discovery falls back to
OpenStreetMap, geocoding to Nominatim, and AI enrichment is skipped. You get
fewer businesses and no ratings or summaries, but every other stage is
identical. Add keys when you want the quality.

## 4. Initialize

```bash
alembic upgrade head    # create the schema
leadgen geo seed        # load the starter gazetteer
leadgen config check    # verify everything
```

`config check` reports exactly what is and isn't wired up:

```
✓ Config files valid (37 niches, 12 locations)
✓ Google Maps API key present
✓ AI enrichment enabled (claude-sonnet-5)
✓ Database reachable
✓ Playwright installed
```

## 5. First run

Start small to confirm the whole path works before spending real quota:

```bash
leadgen audit https://some-local-business.com     # no database needed
leadgen geo plan pasadena --niche plumbing        # preview cells and cost
leadgen run-daily --location pasadena --niche plumbing --target 10
leadgen serve                                      # http://localhost:8000
```

---

## API keys

### Google Maps Platform (recommended)

Powers discovery, geocoding, and PageSpeed.

1. Create a project at <https://console.cloud.google.com/>
2. Enable **Places API (New)**, **Geocoding API**, and **PageSpeed Insights API**
3. Create an API key under *Credentials*
4. **Restrict the key** to exactly those three APIs
5. **Set a billing budget with alerts** before your first large run

Cost, at pricing current when this was written — verify before relying on it:
Nearby Search is roughly $32 per 1,000 calls. A 25-mile radius run across one
niche is around 45 calls, so about $1.50. `leadgen geo plan` projects the cost
of any target before you spend it.

Google's free tier historically covers a meaningful share of small-scale usage;
check your current terms.

### Anthropic (optional)

Powers business summaries, redesign opportunities, and outreach angles. Get a
key at <https://console.anthropic.com/>.

Only the top N leads are enriched (`ai_enrich_top_n`, default 50), and unchanged
businesses are skipped via input hashing, so steady-state cost is low. A hard
ceiling is enforced in-process by `LEADGEN_AI_DAILY_TOKEN_BUDGET`.

### SerpAPI (optional)

Adds Google Maps *ranking* as a signal — a business sitting at #17 for its own
core keyword has a visibility problem worth selling against. Nothing depends on
it.

---

## Environment variables

### Core

| Variable | Default | Notes |
|---|---|---|
| `LEADGEN_ENV` | `development` | `production` enforces extra guardrails at boot |
| `LEADGEN_LOG_LEVEL` | `INFO` | |
| `LEADGEN_LOG_FORMAT` | `console` | Use `json` in production |
| `LEADGEN_TIMEZONE` | `America/Los_Angeles` | Used by the built-in scheduler |
| `LEADGEN_CONFIG_DIR` | `./config` | Where the YAML catalogs live |

### Database

| Variable | Default |
|---|---|
| `LEADGEN_DATABASE_URL` | `postgresql+asyncpg://leadgen:leadgen@localhost:5432/leadgen` |
| `LEADGEN_DB_POOL_SIZE` | `10` |
| `LEADGEN_DB_MAX_OVERFLOW` | `20` |

Alembic derives a sync URL automatically; override with `ALEMBIC_DATABASE_URL`
if your migration user differs.

### Discovery

| Variable | Default |
|---|---|
| `LEADGEN_GOOGLE_MAPS_API_KEY` | — |
| `LEADGEN_PAGESPEED_API_KEY` | falls back to the Maps key |
| `LEADGEN_SERPAPI_KEY` | — |
| `LEADGEN_DISCOVERY_PROVIDERS` | `google_places,osm` |

### AI

| Variable | Default |
|---|---|
| `ANTHROPIC_API_KEY` | — |
| `LEADGEN_AI_MODEL` | `claude-sonnet-5` |
| `LEADGEN_AI_MODEL_PREMIUM` | `claude-opus-5` |
| `LEADGEN_AI_ENABLED` | `true` |
| `LEADGEN_AI_MAX_CONCURRENCY` | `4` |
| `LEADGEN_AI_DAILY_TOKEN_BUDGET` | `2000000` |

### Pipeline

| Variable | Default | Notes |
|---|---|---|
| `LEADGEN_DAILY_LEAD_TARGET` | `50` | Qualified leads wanted |
| `LEADGEN_DAILY_DISCOVERY_CAP` | `2000` | Hard ceiling on businesses fetched |
| `LEADGEN_MAX_CONCURRENT_AUDITS` | `8` | |
| `LEADGEN_AUDIT_TIMEOUT_SECONDS` | `45` | |
| `LEADGEN_ENABLE_BROWSER_AUDIT` | `true` | `false` is ~5× faster, loses mobile checks |
| `LEADGEN_ENABLE_SCREENSHOTS` | `true` | |
| `LEADGEN_SCREENSHOT_DIR` | `./data/screenshots` | |
| `LEADGEN_SCHEDULE_HOUR` | `6` | For the built-in scheduler |

### Politeness

| Variable | Default | Notes |
|---|---|---|
| `LEADGEN_USER_AGENT` | `leadgen-prospect-bot/1.0 (+https://example.com/bot)` | **Change this** to a real contact URL |
| `LEADGEN_RESPECT_ROBOTS_TXT` | `true` | Leave it on |
| `LEADGEN_PER_HOST_RPS` | `0.5` | Requests/second to any one host |
| `LEADGEN_GLOBAL_RPS` | `12` | |
| `LEADGEN_CRAWL_MAX_PAGES_PER_SITE` | `5` | |

### API

| Variable | Default | Notes |
|---|---|---|
| `LEADGEN_API_HOST` | `0.0.0.0` | |
| `LEADGEN_API_PORT` | `8000` | |
| `LEADGEN_API_KEY` | — | **Required in production**; guards all writes |
| `LEADGEN_DASHBOARD_ENABLED` | `true` | |
| `LEADGEN_CORS_ORIGINS` | — | Comma-separated |

### Exports

| Variable | Purpose |
|---|---|
| `LEADGEN_EXPORT_DIR` | Output directory |
| `LEADGEN_GOOGLE_SHEETS_CREDENTIALS_FILE` | Service-account JSON path |
| `LEADGEN_GOOGLE_SHEETS_SPREADSHEET_ID` | Target sheet |
| `LEADGEN_NOTION_API_KEY` / `LEADGEN_NOTION_DATABASE_ID` | Notion |
| `LEADGEN_AIRTABLE_API_KEY` / `LEADGEN_AIRTABLE_BASE_ID` / `LEADGEN_AIRTABLE_TABLE_NAME` | Airtable |

---

## Export destination setup

### Google Sheets

1. In Google Cloud, enable the **Google Sheets API** and **Google Drive API**
2. Create a service account and download its JSON key
3. Point `LEADGEN_GOOGLE_SHEETS_CREDENTIALS_FILE` at that file
4. **Share the target spreadsheet with the service account's email address** —
   this is the step everyone misses, and the resulting "caller does not have
   permission" error does not mention it

### Notion

1. Create an integration at <https://www.notion.so/my-integrations>
2. Share your target database with it
3. The database needs these properties: `Name` (title), `Lead Score` (number),
   `Phone`, `Email`, `Website` (url), `Website Status` (select), `City` (text),
   `State` (select), `Niche` (select), `Rating` (number), `Reviews` (number),
   `Why` (text), `Outreach Angle` (text), `Status` (select)

### Airtable

Create a personal access token with `data.records:write` on your base. Field
names are mapped in `exports/integrations.py:AIRTABLE_FIELD_MAP`; the exporter
sends `typecast: true` so a partially-built schema still accepts what it can.

---

## Expanding the gazetteer

The bundled `config/seed_places.csv` holds 254 real US places, weighted toward
California. That covers city, county, and radius targets well. For genuine
statewide or nationwide runs, import a full gazetteer:

```bash
# SimpleMaps "US Cities" basic CSV (free, ~30k places) — column defaults match
leadgen geo import uscities.csv --min-population 1000

# US Census Gazetteer files work too, with explicit column names
leadgen geo import gaz_places.csv \
  --name-col NAME --state-col USPS --lat-col INTPTLAT --lng-col INTPTLONG
```

Without a full gazetteer, `nation`-scoped targets cannot resolve and will log a
clear error telling you so.

---

## Troubleshooting

**`playwright: executable doesn't exist`** — run
`python -m playwright install chromium`. In Docker, use the provided image.

**`connection refused` on Postgres** — check `docker compose ps` and that
`LEADGEN_DATABASE_URL` matches the port you published.

**`Google Places rejected the request (403)`** — the key is invalid, Places API
(New) is not enabled, or a key restriction excludes it. Note that Places API
(New) is a *separate* API from the legacy Places API.

**`no search cells resolved`** — run `leadgen geo seed`, or the location target
references a county or city not in your gazetteer.

**Zero leads qualified** — the bar is `qualification_threshold: 55` in
`config/scoring.yml`. Check `leadgen stats`; if plenty of businesses were
discovered but none qualified, either the area genuinely has good websites or
the threshold is too high for your market.
