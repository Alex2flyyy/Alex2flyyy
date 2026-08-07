# PawParty

An automated content pipeline for a faceless short-form video account: AI-generated
kittens and puppies dancing, roughly seven unique videos a day, with captions,
hashtags, on-screen text, music, a posting schedule, and quality control — all
from one command.

```bash
pawparty run --count 7 --schedule --export
```

**It runs end to end with no API keys.** Out of the box it uses offline `synth`
providers that build real, playable, correctly-specified 1080×1920 MP4s with
ffmpeg. Those are *previews*, not cute animals — they exist so you can exercise
and tune the entire system (timing, cuts, music mix, text placement, QC,
scheduling) before spending a cent. Point one config line at a real video model
and the same pipeline produces publishable footage.

---

## Table of contents

- [What it actually does](#what-it-actually-does)
- [Install](#install)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Folder structure](#folder-structure)
- [How a video gets made](#how-a-video-gets-made)
- [Configuration](#configuration)
- [Adding a second channel](#adding-a-second-channel)
- [What needs your credentials](#what-needs-your-credentials)
- [Costs](#costs)
- [Further reading](#further-reading)

---

## What it actually does

Each run:

1. **Invents concepts.** Draws from a tagged vocabulary of 200+ themes, settings,
   dance styles, breeds, costumes, palettes, camera moves, effects and music
   vibes — constrained so combinations stay coherent (no Santa hats at the beach)
   and scored against a persistent ledger so nothing repeats.
2. **Writes the prompts.** Per-beat video prompts, image prompts, and a negative
   prompt, all from editable Jinja2 templates. Every prompt is archived.
3. **Generates footage.** One clip per beat, via whichever provider you configured.
4. **Adds camera motion.** Normalises to 9:16 and layers a real camera move
   (push, orbit, handheld float, rhythmic punch-in) on top of the generated clip.
5. **Cuts it together.** Beat-accurate stitching with transitions, then closes the
   loop so the ending flows back into the first frame.
6. **Scores it.** A music bed matched to the concept's vibe and BPM, loudness-
   normalised to the platform target.
7. **Writes the copy.** Five ranked caption options, a pinned-comment seed,
   alt text, and a tiered hashtag set.
8. **Burns in on-screen text.** A hook card at 0s, a mid-video nudge at the
   drop-off point, a comment-bait card over the loop — all inside the safe area.
9. **Checks its own work.** Probes the finished file for duration drift, wrong
   resolution, missing audio, black frames, and absurd file sizes. A failed video
   never reaches `Finished/`.
10. **Schedules and packages it.** Spread across the day with jitter, exported as
    a post-ready folder.

Everything is resumable. Every stage writes its state to `manifest.json`, so a
crashed run picks up exactly where it stopped rather than re-rendering (and
re-paying for) work that already succeeded.

---

## Install

**Requirements:** Python 3.10+ and ffmpeg 5.0+ (with ffprobe).

```bash
# 1. ffmpeg
brew install ffmpeg                     # macOS
sudo apt-get install -y ffmpeg          # Debian / Ubuntu
winget install Gyan.FFmpeg              # Windows

# 2. PawParty
git clone <this-repo>
cd tiktok-paw-party
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Check
pawparty doctor
```

`pawparty doctor` prints one screen covering ffmpeg, config, providers,
credentials, disk space and library size. Start there whenever something is off.

Optional, only if you plan to use paid providers:

```bash
cp .env.example .env    # then fill in the keys you actually have
```

---

## Quick start

```bash
# See what it would make — free, instant, no rendering
pawparty ideas --count 7

# Render two preview videos end to end
pawparty run --count 2

# The full daily run: 7 videos, scheduled, packaged for posting
pawparty run --count 7 --schedule --export
```

Finished videos land in `Videos/Finished/<date>/<concept-id>/final.mp4`, and the
post-ready bundles in `Videos/Scheduled/<date>/upload/`.

To reproduce an exact batch (same concepts, same everything), pass `--seed`:

```bash
pawparty run --count 7 --seed 20260806
```

---

## Commands

| Command | What it does |
|---|---|
| `pawparty doctor` | Environment, config, credential and disk check |
| `pawparty init` | Create the workspace folder tree |
| `pawparty channels` | List channels and the size of each creative space |
| `pawparty wire <model>` | Read a model's schema and propose its provider config |
| `pawparty ideas -n 7` | Generate concepts only — free, no rendering |
| `pawparty run -n 7` | The daily batch: generate + render + QC |
| `pawparty build <id>` | Resume or re-render one video |
| `pawparty schedule` | Plan posting slots for a day |
| `pawparty publish --manual` | Export a post-ready upload bundle |
| `pawparty costs` | Provider spend report |
| `pawparty stats` | Novelty ledger and library statistics |
| `pawparty clean --keep-days 14` | Delete old intermediates (never touches `Finished/`) |

Useful flags: `--channel <name>`, `--seed <n>`, `--date YYYY-MM-DD`,
`-j/--concurrency <n>`, `--occasion halloween`, `--stop-after clips`, `-v`.

Re-rendering just the final encode of one video:

```bash
pawparty build disco-night-kittens-puppies-a1b2c3d4 --from-stage package
```

---

## Folder structure

```
tiktok-paw-party/
├── config/
│   ├── settings.yaml              # how the machine runs
│   ├── settings.local.yaml        # optional git-ignored overrides
│   └── channels/
│       ├── kittens_puppies.yaml   # the flagship channel's vocabulary
│       └── cats_only.yaml         # a second channel, ~50 lines via `extends`
├── pawparty/
│   ├── cli.py                     # command line interface
│   ├── pipeline.py                # the orchestrator
│   ├── config.py  models.py  storage.py  budget.py  util.py
│   ├── ideas/                     # concept generation
│   │   ├── vocabulary.py          #   constrained weighted sampling
│   │   ├── structure.py           #   beat planning / retention spine
│   │   ├── concept.py             #   puts a concept together
│   │   └── novelty.py             #   the anti-repetition ledger
│   ├── prompts/                   # Jinja2 prompt templates + renderer
│   ├── copywriting/               # captions, hashtags, on-screen text
│   ├── providers/                 # pluggable video / image / music / LLM
│   │   ├── video/  image/  music/  llm/
│   │   └── registry.py            #   name -> class, plus fallback policy
│   ├── media/                     # ffmpeg wrapper, motion, assembly, QC
│   ├── scheduling/                # posting slot planning
│   └── publishing/                # manual export + TikTok API client
├── tests/                         # 179 tests, incl. real ffmpeg renders
├── docs/                          # architecture, providers, strategy, legal
├── scripts/                       # install + daily cron entry point
└── Videos/                        # ← all output (git-ignored)
    ├── Generated/                 # raw + prepared per-beat clips
    ├── Images/                    # first frames, covers
    ├── Audio/                     # library/ (your licensed music), beds/
    ├── Captions/                  # caption options, hashtags, subtitles
    ├── Prompts/                   # every prompt sent, archived
    ├── Logs/                      # JSONL run logs, cost ledger, novelty DB
    ├── Scheduled/                 # posting queue + upload bundles
    └── Finished/                  # the deliverables
```

Work is partitioned by date and concept id, e.g.
`Videos/Generated/2026-08-06/disco-night-kittens-puppies-a1b2c3d4/`, so a day's
output is easy to inspect and a retry never collides with a previous attempt.

---

## How a video gets made

```
  ConceptGenerator          PromptRenderer         Providers
  ───────────────           ──────────────         ─────────
  vocabulary sampling  ───▶ per-beat prompts  ───▶ stills ─▶ clips
  + novelty ledger                                            │
  + beat planning                                             ▼
                                                       media/assemble
                                                       ├─ normalise 9:16
                                                       ├─ camera motion
                                                       ├─ transitions
                                                       └─ close the loop
                                                              │
        copywriting ──▶ captions / hashtags / cards ──────────┤
        providers.music ──▶ bed, loudness-normalised ─────────┤
                                                              ▼
                                                        final encode
                                                              │
                                                          media/qc
                                                              │
                                                    Finished/ + Scheduled/
```

Each box is a stage recorded in `manifest.json`. Completed stages are skipped on
a re-run, which is what makes `pawparty build <id>` cheap.

**The retention spine.** Every video is planned around the same structure, with
jittered timings so no two feel stamped out:

| Beat | When | Job |
|---|---|---|
| `hook` | 0–4s | Motion already in progress on frame one. On-screen text asks or claims something. |
| `build` | middle | Escalation — each beat adds a new visual fact. |
| `switch` | ~60–70% | A pattern interrupt, placed where drop-off peaks. |
| `payoff` | last ~25% | The reward the hook promised. |
| `loop` | final ~1s | Rhymes with frame one, so the restart is seamless. |

Length is drawn from four bands (9–13s, 14–21s, 22–32s, 33–45s) rather than one
fixed duration, so the library stays varied and the account's retention profile
stays broad. Beat count is derived from length to keep every beat at ~5 seconds
— the range current video models can actually generate in one shot.

---

## Configuration

**`config/settings.yaml`** controls how the machine runs: render spec, which
providers to use, budget caps, QC thresholds, posting times, subtitle styling.

**`config/settings.local.yaml`** (optional, git-ignored) is deep-merged on top.
Put machine-specific choices there — your model, your budget, your posting times
— so pulling updates to the shared config never causes a merge conflict.
`pawparty wire --apply` writes to it.

**`config/channels/*.yaml`** controls what it makes. Every option is a small
block:

```yaml
- id: santa_hat
  label: "a tiny Santa hat"
  tags: [holiday, headwear]
  requires: [christmas]     # only offered once the scene is Christmassy
  excludes: [underwater]    # never offered underwater
  weight: 1.0               # relative likelihood
```

Sampling walks the pools in order, and each pick adds its tags to a growing
context. Later pools only see options whose `requires` are satisfied and whose
`excludes` don't collide. That's how the combinatorics stay enormous without
producing nonsense.

**To add content, append entries.** No code changes. New options are picked up
immediately and, because they carry no recency penalty, the system will actively
favour them until they've had their turn.

---

## Adding a second channel

Channels inherit, so a new faceless account is a short file:

```yaml
# config/channels/cats_only.yaml
extends: kittens_puppies

display_name: "Kitten Club"

vocabulary_remove:
  animals: [corgi_puppy, golden_retriever_puppy, pug_puppy, ...]

hashtags:
  core: [catsoftiktok, aicats, dancingcats]
```

```bash
pawparty run --channel cats_only --count 5
```

Each channel keeps its own novelty history, so they never converge on the same
content. `pawparty channels` reports the size of each channel's creative space.

---

## What needs your credentials

Being blunt about this, because the difference matters:

| Capability | Works out of the box? | What you need |
|---|---|---|
| Concepts, prompts, structure | **Yes** | Nothing |
| Captions, hashtags, on-screen text | **Yes** | Nothing (LLM optional) |
| Editing, motion, transitions, loop, mix, QC | **Yes** | ffmpeg |
| Preview video (`synth`) | **Yes** | Nothing |
| **Real AI animal footage** | **No** | A video model account — Replicate or fal.ai (`pawparty wire` does the setup) |
| **Licensed music** | **No** | Your own tracks; see [docs/LEGAL.md](docs/LEGAL.md) |
| LLM-written captions | No | `ANTHROPIC_API_KEY` (optional; templates work well) |
| **Automated posting** | **No** | An *approved* TikTok developer app — see [docs/PUBLISHING.md](docs/PUBLISHING.md) |

Two of these deserve emphasis:

- **Automated TikTok posting is not something this project can grant you.** The
  Content Posting API requires a developer app that TikTok reviews and approves
  manually, with no guaranteed outcome. The client is implemented and ready, but
  until you're approved, `pawparty publish --manual` produces a folder you upload
  by hand in about thirty seconds per video. That path always works.
- **The generated music is a placeholder.** It is correct in tempo and length so
  you can validate the mix, but for a public account you want licensed library
  music. Drop your files in `Videos/Audio/library/` and switch
  `providers.music` to `local_library`.

Also: **every video must be labelled as AI-generated when you post it.** The
export bundle puts that on the checklist for each video.

---

## Costs

The pipeline enforces two caps *before* each provider call, so a config typo
can't run up a bill overnight:

```yaml
budget:
  daily_usd: 6.00
  per_video_usd: 1.25
  abort_on_exceed: true
```

Video generation dominates; captions cost fractions of a cent. At 7 videos a day
of 4–6 beats each, expect roughly 30–40 clip generations daily — check your
chosen model's per-second price and set `cost_per_second_usd` to match so the
estimates mean something. `pawparty costs` reports actual spend.

---

## Further reading

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, data model, why the design is shaped this way |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | Wiring up a real video model, step by step |
| [docs/STRATEGY.md](docs/STRATEGY.md) | The retention, hook and posting logic, and what to measure |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | TikTok API approval, OAuth, and the manual path |
| [docs/LEGAL.md](docs/LEGAL.md) | Music licensing, AI disclosure, platform policy |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Daily automation, troubleshooting, common failures |

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # 179 tests
pytest -m "not ffmpeg"      # skip the real-render tests
```

The suite runs against the real config, and the ffmpeg-marked tests render
actual video and probe the result — including a regression test for the stitch
bug where a sub-frame transition silently truncated a 42-second video to 20.

## Licence

MIT.
