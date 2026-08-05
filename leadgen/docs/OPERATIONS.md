# Operations

How to run this day to day, tune it, and fix it when it misbehaves.

## The daily loop

The pipeline runs overnight. Your morning is:

```bash
open data/exports/reports/leads_$(date +%F).html
```

or the dashboard at `/`. Work the list top to bottom — it is already ordered by
how likely each prospect is to buy. As you go, update each lead's status from
its detail page so tomorrow's report does not re-surface people you have already
called.

Two views worth checking weekly:

```bash
curl -s localhost:8000/api/stats/website-changes | jq
```

A prospect whose site **broke** in the last week is the warmest call available —
they already know something is wrong. A prospect whose site **improved** was
sold by someone else; mark them lost and stop spending calls on them.

```bash
curl -s localhost:8000/api/leads/follow-ups | jq
```

Anything you set a follow-up date on that has come due.

---

## Tuning

### "I'm not getting enough leads"

Check where the funnel is narrowing before changing anything:

```bash
leadgen stats
```

| Symptom | Cause | Fix |
|---|---|---|
| Few discovered | Search area too small | Raise `max_cells`, or move to a county target |
| Many discovered, few new | Area exhausted | Rotate location or niche; the deduper is working correctly |
| Many new, few qualified | Bar too high, or good local websites | Lower `qualification_threshold` to ~45 and inspect the results |
| Many qualified, few contactable | Missing contact data | Enable contact enrichment; raise `crawl_max_pages_per_site` |

An exhausted area is normal and healthy — it means you have already found
everyone. Rotate:

```bash
# Monday: plumbing in LA county.  Tuesday: roofing.  etc.
leadgen run-daily --location los_angeles_county --niche roofing
```

### "The leads aren't good"

Look at a low-quality lead's detail page and read its score breakdown. The
component with the largest contribution is the one to adjust in
`config/scoring.yml`.

Common adjustments:

- Getting franchises → add name patterns to `chain_signals.name_patterns`
- Getting businesses you cannot reach → raise `no_contact_channel_penalty`
- Getting tiny businesses that cannot pay → raise the `revenue_potential`
  weight, or raise `reviews_floor`
- Getting sites that are actually fine → raise `poor_below` so fewer sites are
  classed as poor

Weights must still sum to 1.0; config loading validates this and fails loudly if
not.

### "It's too slow"

The audit stage is the bottleneck at every scale. In order of impact:

```bash
--no-pagespeed                      # removes 10-30s per site
LEADGEN_ENABLE_BROWSER_AUDIT=false  # ~5× faster, loses mobile and screenshots
LEADGEN_MAX_CONCURRENT_AUDITS=16    # more parallelism
```

A good pattern at volume: run the bulk pass HTTP-only, then re-audit just the
top 50 with the full suite. `leadgen run-daily --no-pagespeed --no-browser`
followed by a targeted second pass gets you most of the signal for a fraction of
the wall clock.

Do **not** raise `LEADGEN_PER_HOST_RPS`. It is 0.5 for a reason: a local
plumber's site is often on shared hosting where a burst of concurrent requests
is indistinguishable from an attack.

### "It's too expensive"

```bash
leadgen geo plan <location> --niche <niche>
```

projects the spend before you commit. Then:

- Lower `LEADGEN_DAILY_DISCOVERY_CAP`
- Raise `cell_radius_m` (fewer, larger cells — cheaper, but a dense urban cell
  will hit the 20-result ceiling and silently miss businesses)
- Drop `google_places` from `LEADGEN_DISCOVERY_PROVIDERS` and run OSM-only for a
  bulk pass
- Lower `ai_enrich_top_n`
- Lower `LEADGEN_AI_DAILY_TOKEN_BUDGET` as a hard stop

---

## Scaling up

| Target | Location scope | Settings |
|---|---|---|
| 50/day | radius, 25 mi | Defaults |
| 500/day | county | `max_cells: 90`, cap 5000, concurrency 16 |
| 5,000/day | state | `max_cells: 150`, cap 50000, `--no-pagespeed`, concurrency 32, then re-audit the top slice |

Above ~1,000/day, three things start to matter:

1. **Provider quota.** Request a higher Places limit before you need it.
2. **Database size.** At 5,000/day you add ~1.8M businesses a year. Postgres
   handles that fine, but keep an eye on `website_audits`, which grows fastest.
   Prune audits older than a year for businesses you never contacted.
3. **Report length.** Nobody works a 5,000-row list. Keep the daily report at
   50 and use the dashboard filters for everything else.

---

## Maintenance

### Weekly

```bash
leadgen stats                         # funnel still moving?
curl -s localhost:8000/api/runs | jq '.[0] | {status, discovered, qualified, errors}'
```

Check that recent runs are `completed`, not `partial`. A `partial` run has
errors recorded on it — read them.

### Monthly

- Review conversion by niche (`/api/stats/by-niche`) and drop niches that never
  convert; every wasted call has a cost
- Re-audit long-untouched leads — the pipeline already rolls a slice of the
  stalest records into each run via `recheck_stale_after_days`, but you can raise
  `recheck_budget` for a catch-up pass
- Review the suppression list
- Check API spend against your budget alerts

### Quarterly

- `pip install -e ".[dev]" --upgrade` and run the test suite
- `python -m playwright install chromium` — the detection heuristics assume a
  reasonably current browser
- Revisit `config/scoring.yml` against actual closed deals. This is the highest-value
  maintenance task in the whole system: your real win rate by segment is better
  evidence than any weight I guessed at.
- Prune old screenshots (`data/screenshots/`) — they accumulate quietly

### Database hygiene

```sql
-- Where is the space going?
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC;

-- Prune audit history for leads you never pursued
DELETE FROM website_audits
WHERE checked_at < now() - interval '1 year'
  AND business_id IN (
    SELECT business_id FROM leads WHERE status IN ('new', 'disqualified')
  );
```

---

## Troubleshooting

**Run fails immediately** — `leadgen config check`. Almost always the database
URL or a missing migration.

**`no search cells resolved`** — the location target references a place not in
your gazetteer. Run `leadgen geo seed`, or import a full one. Verify with
`leadgen geo plan <location>`.

**Every audit returns `dns_failure`** — check outbound network access from the
host. In Docker, confirm DNS resolution inside the container.

**Playwright crashes or hangs** — usually memory. Chromium needs ~1 GB per
concurrent context; lower `LEADGEN_MAX_CONCURRENT_AUDITS`. If `/dev/shm` errors
appear, confirm `--disable-dev-shm-usage` is being passed (it is, by default).

**Duplicate businesses appear** — inspect their `dedupe_key` and
`normalized_name`. The usual cause is genuinely different street addresses for
the same business (a suite move, or one provider listing a mailing address).
Add a normalization rule in `enrichment/normalize.py` and add a test for it.

**AI enrichment produces nothing** — check `ANTHROPIC_API_KEY` and
`LEADGEN_AI_ENABLED`. Note that `skipped_cached` in the run's `ai_enrich` stage
notes is *correct behavior*: unchanged businesses are deliberately not
re-analyzed.

**Scores all cluster at one value** — usually a config error where one weight
dominates. `leadgen config show` and inspect a lead's breakdown.

### Reading a failed run

```bash
curl -s localhost:8000/api/runs/latest | jq '{status, errors, stages}'
```

Each stage records `processed`, `succeeded`, `failed`, `skipped`, and duration.
The stage where `failed` spikes is where to look. With JSON logging on, filter
by `run_id` to see everything that run emitted.

---

## Backup and restore

```bash
# Back up
docker compose exec -T postgres pg_dump -U leadgen leadgen | gzip > backup.sql.gz

# Restore
gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U leadgen leadgen
```

Screenshots and reports under `data/` are regenerable and do not need backing
up. The database is the only irreplaceable thing — it holds your discovery
history and, more importantly, your sales notes.
