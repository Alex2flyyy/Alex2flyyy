# QA sheet — loop-beat reuse

Private release-readiness audit. **No paid provider was called, no credits
spent, nothing scheduled or published, no account settings touched.** Every
figure below comes from local ffmpeg renders using the offline `synth`
providers, or from arithmetic on published billing rules.

- Baseline commit: `d1457c4`
- Re-runnable with: `.venv/bin/python scripts/qa_audit.py --out <dir> --seed 31`
- Raw data: `qa_report.json` alongside the preview MP4s

---

## 1. Read this before the SSIM numbers

The seam numbers below are measured on **`synth` placeholder footage** —
animated colour fields with the prompt drawn on top. They are evidence about
**one thing only**: that reusing beat one's opening frames puts the closing
frames closer to the opening frames than generating a separate clip does.

They are **not** evidence about:

- how the finished videos will look with a real video model;
- whether the loop is perceptually seamless to a viewer;
- creative quality, cuteness, or whether anyone will watch.

A placeholder generator has no animal, no costume and no pose to drift, so it
*understates* the mechanism's benefit — independently generated real footage
drifts far more than two colour gradients do. But it cannot tell you the final
figure either. **Re-measure on real footage before trusting any number here as
a quality claim.**

---

## 2. Side-by-side: same concept, rendered both ways

Seed 31, concept `ninja-dojo-puppies-hip-hop-bounce-2d83460c`,
"Ninja dojo — Puppies hip-hop bounce", 4 beats, 16.9s.

| | BEFORE (`reuse_loop_clip: false`) | AFTER (`reuse_loop_clip: true`) | |
|---|---|---|---|
| **Clip requests** | 4 generated, 0 derived | **3 generated, 1 derived** | −1 request |
| **Still requests** | 4 | **3** | −1 request |
| Deliverable | 16.9s · 1080×1920 · 30fps · h264/aac | 16.9s · 1080×1920 · 30fps · h264/aac | identical spec |
| File size | 8.91 MB | 8.69 MB | −2.5% |
| QC | passed, 0 warnings | passed, 0 warnings | no regression |
| **Seam, last vs first frame** | SSIM 0.7022 | **SSIM 0.8602** | +0.158 |
| **Seam, 6-frame window mean** | SSIM 0.6942 | **SSIM 0.8495** | +0.155 |
| Caption cards | 3, hook at 0.00s | 3, hook at 0.00s | unchanged |
| Last card ends | 16.75s (0.15s headroom) | 16.75s (0.15s headroom) | unchanged |
| Audio head / mid / tail | −25.1 / −14.8 / −32.5 dBFS | −25.1 / −14.8 / −32.5 dBFS | bit-identical |
| Fade in / out depth | 10.3 dB / 17.7 dB | 10.3 dB / 17.7 dB | unchanged |

**Audio is byte-identical between the two variants** (verified by stream MD5),
which is the expected result: loop reuse touches video only.

### Seam at phone playback speed

A single last-vs-first frame pair is a weak signal — a fade can flatter it. The
window below walks the final 6 frames against the first 6, which at 30fps is
**200 ms** of real playback, or about 6 frames at 33.3 ms each.

| frame offset from loop point | BEFORE | AFTER |
|---|---|---|
| −6 | 0.7307 | 0.8382 |
| −5 | 0.7348 | 0.8411 |
| −4 | 0.7472 | 0.8556 |
| −3 | 0.7472 | 0.8557 |
| −2 | 0.7441 | 0.8526 |
| −1 | 0.7452 | 0.8535 |

The improvement holds across the whole window rather than at one lucky frame,
so it is the approach to the loop that converged, not a single sample.

---

## 3. Checks run

| Check | Result |
|---|---|
| Estimator arithmetic vs independent hand calculation, beat counts 2–8 | **PASS** (7/7 buckets) |
| `requests + avoided == total beats`, every bucket | **PASS** |
| Stills charged once per generated beat, never for a derived beat | **PASS** |
| Billing block boundaries (0.7 / 4.99 / 5.0 / 5.01 / 6.0 / 6.01 / 8.0 / 8.01 s) | **PASS** |
| Billing never understates within the model's supported range | **PASS** |
| `doctor` daily range brackets five real preflight days | **PASS** ($7.22–$11.07 vs actual $7.83–$8.81) |
| Derivation eligibility: 2–3 beats → 0 derived; 4+ → exactly 1 | **PASS** (400 concepts) |
| Fallback: beat 1 corrupt (non-empty, undecodable) | **PASS** — discarded, regenerated, QC passed |
| Fallback: beat 1 missing | **PASS** — regenerated, QC passed |
| Fallback: beat 1 zero bytes | **PASS** — regenerated, QC passed |
| Fallback: beat 1 truncated to 0.30s (loop needs 1.60s) | **PASS** — derivation refused, beat generated, QC passed |
| Caption cards inside the video, no overlap, hook at frame one | **PASS** |
| Audio fade-in present | **PASS** (10.3 dB) |
| Audio fade-out present | **PASS** (17.7 dB) — *was absent before this audit* |
| Deterministic rerun, byte-identical `final.mp4` | **PASS** — *was failing before this audit* |
| Full test suite | **243 passed** |
| `pawparty doctor` (free providers) | **all checks passed**, exit 0 |

---

## 4. Defects this audit found and fixed

Five, two of them release-blocking.

**1. Cached clips were trusted on file size alone.** `_stage_clips` reused any
existing non-empty file without checking it was decodable. A download cut short
by a timeout or a full disk leaves plenty of bytes and no `moov` atom; the
resume path reused it and the job then died three stages later in `prepare_beat`
with an ffmpeg error pointing at the wrong place. Every subsequent
`pawparty build` on that job failed the same way until someone manually found
and deleted the file. Now validated via `ffmpeg.is_decodable`, discarded and
regenerated with a loud warning. *Pre-existing; not introduced by loop reuse.*
**Release-blocking.**

**2. `--seed` did not reproduce preview footage.** ffmpeg's `gradients` filter
seeds itself randomly (`seed=-1`) unless told otherwise, and the `synth`
providers never set it. The concept was reproducible; the footage was not —
contradicting what the README promises about `--seed`. Now seeded from the same
deterministic RNG. **Release-blocking for reproducible QA.**

**3. The music bed had no fade-out.** Only `afade=t=in` was applied. Audio
stopped at full volume, so the restart clicked audibly — at exactly the seam
the loop beat exists to hide. Now symmetric, configurable
(`render.audio_fade_in_s` / `audio_fade_out_s`, default 0.22s each).

**4. `doctor` under-estimated the daily cost.** It sampled one batch through a
different code path (sequential seeds, no novelty ledger) and reported $7.22/day
while real days ran $7.84–$8.81. Under-estimating is the dangerous direction for
a budget check, and it advised raising the cap to $8 when nights hit $8.81. Now
simulates five days and tests the cap against the worst, reporting a range.

**5. Replicate's duration blocks were hard-coded and clamped silently.** Any
beat over 10s was silently requested at 10s — under-billing the estimate and
yielding a short clip that assembly has to retime. Blocks are now configurable
per model (`duration_blocks`), and the over-limit path warns once instead of
hiding. *Unreachable today — role limits cap beats at 9.0s — so this is defence
in depth, with a test that fails if anyone retunes those limits.*

A sixth item was a **bad measurement, not a bug**: the audit's first audio check
used a 250 ms window to measure a 220 ms fade, so a working fade read as no
fade. The window now sits inside the fade.

---

## 5. What is NOT verified by any of this

- **Creative quality.** Nothing here says the videos are good.
- **Real-model seam behaviour.** All seam figures are placeholder footage.
- **Perceptual seamlessness.** SSIM is a structural metric, not a viewer.
- **Real audio.** The bed is a synthesised placeholder; a licensed track has
  different dynamics and the fade may want retuning.
- **Platform behaviour.** Nothing has been uploaded, so nothing is known about
  how TikTok's re-encode treats these files, or how the loop reads in-app.
- **Cost against a real invoice.** Every dollar figure is arithmetic on
  published block pricing with an operator-supplied per-second rate. It has
  never been checked against a bill.

---

## 6. Required before a paid render or a schedule

Nothing below can be done from this session. Each item needs the account
holder.

### Accounts and credentials

| | Needed | Check |
|---|---|---|
| Video model host | Replicate or fal.ai account + API token | Token in `.env`; `pawparty doctor` shows it set |
| Model choice | A current vertical-capable model slug | `pawparty wire <slug> --show-schema`, confirm a 9:16 option exists |
| Billing blocks | The model's real duration enum | From `--show-schema`; set `duration_blocks` if it isn't 5/6/8/10 |
| Per-second price | From the model's pricing page | Set `cost_per_second_usd`; the budget guard is only as good as this |
| Spend ceiling | A number you are willing to see on a bill | Raise `budget.daily_usd` above `doctor`'s **upper** bound, and `per_video_usd` above the longest video's cost — currently $1.25 vs $1.58 needed |
| Payment method | Funded account with a hard spend limit set **at the provider** | PawParty's caps are advisory; only the provider can actually stop charges |

### Real-source assets

| | Needed |
|---|---|
| **Music** | Licensed tracks in `Videos/Audio/library/` + `library.yaml` with a `license` field per track; `providers.music: local_library`. The `synth` bed is a development placeholder and must not ship. |
| **Fonts** | The font named in `subtitles.font` must exist on the render host (`fc-list \| grep -i dejavu`). A missing font renders boxes. |
| **Nothing else** | No stock footage, logos or brand assets are required — all footage is generated. |

### Licences and policy

- **Music licence** covering commercial use on a monetised social account, with
  the invoice or subscription ID recorded. Confirm whether coverage survives
  cancellation — that is where people get caught.
- **Model output terms** permitting commercial use on your plan tier.
- **AI-generated label on every post.** Required by TikTok's synthetic-media
  policy. The API client defaults it on; manual posting has it on the checklist.
- **No "real animal" framing** in captions. The channel config bans it; worth
  re-reading the generated captions once with that in mind.
- See [LEGAL.md](LEGAL.md) for the full position.

### Human review before the first paid batch

1. `pawparty run --count 7 --estimate-only` — read the predicted cost, and
   confirm it sits under your provider-side spend limit.
2. `pawparty run --count 1` with the real provider — **one video, watched end to
   end**, before any batch. Check the animal is coherent across beats, the loop
   reads as a loop in an actual phone-sized player, and the music sits right.
3. **Re-run this audit on real footage**: `scripts/qa_audit.py` with
   `reuse_loop_clip` on and off, and compare the seam numbers. Replace the
   placeholder figures in this sheet with the real ones.
4. Watch **all seven** of the first full batch. Not a sample.
5. Confirm captions and hashtags read as intended for your brand, and that the
   pinned-comment seeds make sense.
6. Only then schedule. Posting is a separate decision from rendering, and
   `pawparty schedule` / `publish` should not run until steps 1–5 pass.

### Before unattended operation

- Alerting on `batch.json` → `failed` and `degraded` (a degraded provider means
  the "finished" videos are placeholders).
- Disk headroom plus `pawparty clean` on a schedule.
- A persistent machine so `Videos/Logs/novelty.sqlite3` survives — losing it
  loses all repetition protection against already-published content.
- See [RUNBOOK.md](RUNBOOK.md).
