# Runbook

Running this daily, and fixing it when it breaks.

---

## The daily loop

```bash
pawparty run --count 7 --schedule --export
```

That generates, renders, QCs, schedules and packages a day's videos. Everything
else in this document is either automating that line or diagnosing it.

---

## Automating it

### cron

```bash
crontab -e
```

```cron
# 05:00 local — render the day's batch before the first post slot.
0 5 * * * /path/to/tiktok-paw-party/scripts/daily_run.sh >> /var/log/pawparty.log 2>&1

# Weekly cleanup of intermediates. Finished/ is never touched.
0 4 * * 0 cd /path/to/tiktok-paw-party && .venv/bin/pawparty clean --keep-days 14
```

`scripts/daily_run.sh` activates the venv, checks disk, runs the batch, and exits
non-zero if any video failed — so a monitoring wrapper can notice.

### systemd timer

```ini
# /etc/systemd/system/pawparty.service
[Unit]
Description=PawParty daily batch
After=network-online.target

[Service]
Type=oneshot
User=pawparty
WorkingDirectory=/opt/tiktok-paw-party
ExecStart=/opt/tiktok-paw-party/scripts/daily_run.sh
```

```ini
# /etc/systemd/system/pawparty.timer
[Unit]
Description=Run PawParty daily

[Timer]
OnCalendar=*-*-* 05:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now pawparty.timer
journalctl -u pawparty -f
```

### GitHub Actions

A template lives at `.github/workflows/daily.yml`. It works, with two caveats
worth understanding before you rely on it:

- **Scheduled workflows are best-effort.** Runner backlog routinely delays them,
  and GitHub disables schedules on repos with no activity for 60 days.
- **Artifacts expire.** The finished videos have to go somewhere durable (S3,
  R2, a release) or they're gone in 90 days.

For a real account, a small always-on box is the better answer. Actions is
genuinely useful for the free preview run as a smoke test on every commit.

---

## Sizing the machine

Rendering is CPU-bound ffmpeg work. Provider generation is wall-clock wait.

| Setup | `concurrency` | Rough time for 7 videos |
|---|---|---|
| 2-core VPS | 1–2 | 15–25 min (synth) |
| 4-core | 3 | 8–12 min |
| 8-core | 4–6 | 4–6 min |

With a real video provider, wall-clock is dominated by generation queues, not
your CPU — expect 20–60 minutes for a batch regardless of cores, and raise
`concurrency` mainly to overlap those waits (watch the provider's rate limits).

**Disk:** roughly 200–400MB per video with intermediates kept, ~15–25MB for the
finished file alone. A week of 7/day with intermediates is ~15GB. Either run
`pawparty clean` weekly or set `keep_intermediates: false`.

---

## Monitoring

Every run writes JSON lines to `Videos/Logs/run-<date>.jsonl` and a summary to
`Videos/Logs/<date>/batch.json`.

```bash
# Did anything fail today?
jq -r 'select(.level=="ERROR") | .message' Videos/Logs/run-$(date +%F).jsonl

# Reconstruct one video's history
jq 'select(.concept=="disco-night-kittens-a1b2c3d4")' Videos/Logs/run-*.jsonl

# The day at a glance
jq '{cost: .total_cost_usd, ok: (.succeeded|length), failed: (.failed|length), degraded: .degraded}' \
   Videos/Logs/$(date +%F)/batch.json

pawparty costs
pawparty stats
```

**Three things worth alerting on:**

1. Any `failed` entry in `batch.json`.
2. `degraded` being non-empty — that means a provider fell back to `synth` and
   your "published" videos are placeholders.
3. Daily spend approaching the cap.

---

## Troubleshooting

### `ffmpeg not found on PATH`

```bash
brew install ffmpeg              # macOS
sudo apt-get install -y ffmpeg   # Debian/Ubuntu
winget install Gyan.FFmpeg       # Windows
pawparty doctor
```

Under cron, `PATH` is minimal — this is the single most common cron-only failure.
`scripts/daily_run.sh` sets an explicit PATH; if you wrote your own wrapper, do
the same or use absolute paths.

### Videos render but look like coloured gradients with text

That's the `synth` provider — expected until you configure a real one. Check:

```bash
pawparty doctor    # look at "providers (configured)" and "credentials"
```

If you *did* configure one and still get gradients, a credential is missing and
the registry degraded. The warning is in the log and in `batch.json`'s
`degraded` field:

```
WARNING video provider 'replicate' unavailable (REPLICATE_API_TOKEN is not set)
        — falling back to 'synth'
```

### `QC FAILED: duration ... drifted ... from the planned ...`

A beat failed to render, or the stitch dropped footage. Inspect:

```bash
D=Videos/Generated/2026-08-06/<concept-id>
for f in $D/prepared/*.mp4; do
  echo -n "$(basename $f) "; ffprobe -v error -show_entries format=duration -of csv=p=0 $f
done
ffprobe -v error -show_entries format=duration -of csv=p=0 $D/stitched.mp4
```

If the prepared clips are right but `stitched.mp4` is short, that's a transition
problem — see the offset rules in
[ARCHITECTURE.md](ARCHITECTURE.md#assembly-mediaassemblepy). If a prepared clip
is wrong, re-render that stage:

```bash
pawparty build <concept-id> --from-stage clips
```

Small drift is normal and expected: transitions consume time from both sides.
That's what `qc.duration_tolerance_s` is for.

### `QC FAILED: mostly black video`

The generation provider returned an empty or near-empty clip — the classic silent
failure. Delete the offending raw clip and rebuild; it'll regenerate:

```bash
rm Videos/Generated/<date>/<concept-id>/raw/beat_03.mp4
pawparty build <concept-id> --from-stage clips
```

If it recurs across videos, the prompt is likely being refused by the model.
Check the archived prompt in `Videos/Prompts/<date>/<concept-id>/prompts.md`.

### `budget: daily spend would reach $X, over the $Y cap`

Working as intended. Either raise the cap or accept fewer videos today:

```bash
PAWPARTY_DAILY_BUDGET_USD=12.00 pawparty run --count 7
```

If it trips much earlier than expected, your `cost_per_second_usd` is probably
wrong for the model you chose — check its pricing page.

### Subtitles render as boxes, or the wrong font

The font in `subtitles.font` must exist on the render host.

```bash
fc-list | grep -i "dejavu"                    # Linux
sudo apt-get install -y fonts-dejavu-core     # if missing
```

Any installed family works — set `subtitles.font` to its exact name.

### Videos look repetitive

```bash
pawparty stats --top 25
```

A runaway entry in the leaderboard is the diagnosis. Fixes, cheapest first:

1. Add options in the over-represented category (append to the channel YAML).
2. Lower that option's `weight`.
3. Raise `decay_window` or `penalty_strength` in `NoveltyConfig`.

### `no completed videos found for <date>`

`pawparty schedule` only picks up jobs whose manifest says `done`. Check:

```bash
jq -r '.status, .error' Videos/Generated/<date>/*/manifest.json
```

### Jobs hang

Provider polling is bounded (`timeout_s`, default 900s) and every ffmpeg call has
a timeout, so a true hang is unusual. If a run is just slow, `-v` shows which
stage each job is in. Interrupting is safe — completed stages are recorded, and
re-running resumes.

---

## Recovery

### Re-render one video

```bash
pawparty build <concept-id>                      # resume where it stopped
pawparty build <concept-id> --from-stage motion  # redo from a stage
pawparty build <concept-id> --from-stage package # just the final encode
```

### Re-run a whole day

Delete the day's `Generated/` partition and run again with the same seed:

```bash
rm -rf Videos/Generated/2026-08-06
pawparty run --count 7 --seed <original-seed> --date 2026-08-06
```

The seed is in each `manifest.json` under `concept.seed`. Note that the novelty
ledger already contains those concepts, so without the seed you'll get different
videos — which is usually what you want anyway.

### Disk full

```bash
pawparty clean --keep-days 7
du -sh Videos/*
```

`clean` never touches `Finished/`. For a permanently tight box, set
`keep_intermediates: false` so each job purges its own working files on success.

### Start the novelty history over

```bash
mv Videos/Logs/novelty.sqlite3 Videos/Logs/novelty.sqlite3.bak
```

Only do this if you've substantially rewritten the vocabulary. You'll lose all
repetition protection against previously published content.

---

## Routine maintenance

| Cadence | Task |
|---|---|
| Daily | Skim `batch.json` for failures and `degraded` |
| Weekly | `pawparty stats` — check for a runaway option; update the `rotating` hashtags |
| Weekly | `pawparty clean --keep-days 14` |
| Monthly | Review `pawparty costs` against actual provider invoices |
| Monthly | Re-tune `schedule.post_times` and band weights from your analytics |
| Quarterly | Re-evaluate your video model — this space moves fast |
| Yearly | TikTok refresh token renewal, if using the API |

---

## Before you leave it unattended

- [ ] `pawparty doctor` is clean.
- [ ] A full batch has run end to end and you've **watched every video**.
- [ ] `providers.video` is a real model, not `synth`.
- [ ] `providers.music` is `local_library` with licensed tracks.
- [ ] Budget caps are set to a number you'd be comfortable seeing on a bill.
- [ ] Logs go somewhere you'll actually look.
- [ ] You have an alert for failures and for `degraded`.
- [ ] Disk has at least a week of headroom, and `clean` is scheduled.
