# Deployment

Three options, in order of how much infrastructure you have to own.

| Option | Best when | Trade-off |
|---|---|---|
| GitHub Actions | You want zero servers | Needs a database reachable from GitHub's runners |
| Docker Compose on a VPS | You want everything in one place | You maintain the box |
| Managed containers | You already run infrastructure | Most setup |

---

## Option 1 — GitHub Actions

The workflow at `.github/workflows/daily-leads.yml` runs the pipeline every
morning, publishes the report as a downloadable artifact, and keeps 90 days of
history. No server to maintain.

### Setup

1. Provision a managed Postgres reachable from the public internet — Neon,
   Supabase, and RDS all work. Free tiers are sufficient at 50 leads/day.

2. Add repository secrets under *Settings → Secrets and variables → Actions*:

   | Secret | Required |
   |---|---|
   | `LEADGEN_DATABASE_URL` | yes — paste your provider's connection string verbatim |
   | `LEADGEN_GOOGLE_MAPS_API_KEY` | recommended |
   | `ANTHROPIC_API_KEY` | optional |
   | `LEADGEN_API_KEY` | if you also run the API |
   | `LEADGEN_AIRTABLE_*`, `LEADGEN_NOTION_*` | optional |
   | `LEADGEN_SLACK_WEBHOOK_URL` | optional, for failure alerts |

3. Run it manually first: *Actions → Daily lead generation → Run workflow*,
   with a small target. Confirm it succeeds before letting the schedule take
   over.

### Notes

The cron is `15 13 * * *` — 06:15 Pacific in summer. **GitHub cron is UTC and
does not follow daylight saving**, so it drifts to 05:15 in winter. Adjust the
hour if that matters.

A `concurrency` group prevents a manual run from colliding with the scheduled
one; two pipelines against one database means doubled API spend for the same
leads.

Reports land as build artifacts. To get them somewhere more useful, configure
Airtable, Notion, or Google Sheets — the workflow pushes to Airtable
automatically when its secrets are present.

---

## Option 2 — Docker Compose on a VPS

Runs Postgres, migrations, the API and dashboard, and the scheduler as one
stack. A 2 GB / 2 vCPU instance handles 50-500 leads/day comfortably.

```bash
git clone <your-repo-url> && cd Alex2flyyy/leadgen
cp .env.example .env      # fill in real values

# Generate real secrets
python -c "import secrets; print('LEADGEN_API_KEY=' + secrets.token_urlsafe(32))" >> .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))" >> .env

docker compose up -d
docker compose logs -f scheduler
```

Services:

- **postgres** — data, on a named volume
- **migrate** — runs `alembic upgrade head` once, then exits; the API waits for
  it to complete successfully
- **api** — dashboard and JSON API on port 8000
- **scheduler** — ticks hourly, fires the pipeline at `LEADGEN_SCHEDULE_HOUR`

Prefer an external scheduler if you have one. The in-container scheduler is
convenient but a restart mid-wait loses that day's slot; a host crontab or
systemd timer does not:

```cron
15 6 * * * cd /srv/leadgen && docker compose run --rm worker >> /var/log/leadgen.log 2>&1
```

### Put it behind TLS

Never expose port 8000 directly. Caddy is the least work:

```caddy
leads.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Then bind the app to localhost only, in `docker-compose.yml`:

```yaml
api:
  ports:
    - "127.0.0.1:8000:8000"
```

### Backups

```bash
# Nightly dump, 30-day retention
0 3 * * * docker compose exec -T postgres pg_dump -U leadgen leadgen \
  | gzip > /backups/leadgen-$(date +\%F).sql.gz
0 4 * * * find /backups -name 'leadgen-*.sql.gz' -mtime +30 -delete
```

Test the restore path before you need it:

```bash
gunzip -c /backups/leadgen-2026-08-05.sql.gz \
  | docker compose exec -T postgres psql -U leadgen leadgen
```

---

## Option 3 — Managed containers

The image builds two targets: `api` and `worker`. Deploy `api` as a
long-running service and `worker` as a scheduled job.

```bash
docker build --target api    -t leadgen-api:1.0 .
docker build --target worker -t leadgen-worker:1.0 .
```

Kubernetes sketch — an API `Deployment` plus a `CronJob`:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: leadgen-daily
spec:
  schedule: "15 13 * * *"
  concurrencyPolicy: Forbid          # never overlap runs
  jobTemplate:
    spec:
      backoffLimit: 1                 # a failed run should not retry blindly
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: worker
              image: leadgen-worker:1.0
              envFrom:
                - secretRef: { name: leadgen-secrets }
              resources:
                requests: { memory: 1Gi, cpu: 500m }
                limits:   { memory: 3Gi, cpu: "2" }
```

Chromium is memory-hungry. Give the worker at least 2 GB, and note that
`/dev/shm` defaults to 64 MB in containers — the launch flags already pass
`--disable-dev-shm-usage` to work around it.

---

## Security

### Secrets

Never commit `.env`; it is gitignored. Use your platform's secret manager in
production. Rotate the Google key and `LEADGEN_API_KEY` on a schedule, and
immediately if a key ever appears in a log or a screenshot.

Restrict the Google key to the three APIs it needs. An unrestricted key found in
a repository gets used by someone else, on your bill.

### API authentication

`LEADGEN_API_KEY` guards every write endpoint. `Settings` refuses to boot in
production without it, so there is no "we forgot" failure mode. The comparison
uses `secrets.compare_digest`, because a plain `!=` leaks key material through
response timing.

Read endpoints are open by design — the dashboard uses them. If your deployment
is internet-facing, put the whole thing behind your identity provider or a VPN;
the lead list is commercially sensitive even though it is built from public
data.

### Database

Use a dedicated role with rights only on the `leadgen` database. Require TLS
(`?ssl=require` for asyncpg). Do not expose port 5432 publicly — in Compose,
bind it to `127.0.0.1`.

### Application hardening

- Runs as non-root (`pwuser`) in the image
- The global exception handler never returns a stack trace to a client; the
  traceback goes to the logs with the request path
- CSV exports escape leading `=`, `+`, `-`, and `@`, because business names come
  from the open web and would otherwise be a formula-injection vector in Excel
- `ignore_https_errors` in the browser context is deliberate: we need to
  *observe* certificate problems, not abort on them. Nothing from an audited
  page is ever executed or trusted.

### Data protection

The database holds business contact information. Treat it as regulated:

- Encrypt at rest (every managed provider offers this)
- Restrict who can query it and who can download exports
- Honor deletion requests — `compliance/policy.py:build_deletion_record`
  produces what you need to service one
- Do not retain leads you will never contact; a smaller database is a smaller
  breach

See [COMPLIANCE.md](COMPLIANCE.md).

---

## Monitoring

`GET /health` returns 200 with database status; point your uptime checker at it.

Log events worth alerting on, all emitted as structured JSON with `run_id`:

| Event | Meaning |
|---|---|
| `pipeline.failed` | The run died — page someone |
| `provider.quota_exhausted` | Out of API budget — leads will thin out |
| `ai.budget_exhausted` | Reports will ship without summaries |
| `geo.gazetteer_empty` | A location target cannot resolve |
| `api.unhandled` | An unexpected server error |

Health checks worth running weekly:

```bash
leadgen stats                              # is the funnel still moving?
curl -s localhost:8000/api/runs | jq '.[0]'  # did last night succeed?
```

A run that "succeeds" with zero new businesses is the failure mode to watch for
— it usually means a provider silently started returning nothing.

---

## Upgrading

```bash
git pull
docker compose build
docker compose run --rm migrate    # migrations first, always
docker compose up -d
```

Migrations are forward-only in practice. Back up before applying any migration
that drops or alters a column. CI verifies that models and migrations have not
drifted, so a schema change without a migration fails the build rather than
production.
