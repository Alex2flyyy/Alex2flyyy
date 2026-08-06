# Architecture

How the system is put together, and why.

---

## The shape of the problem

An unattended pipeline that posts seven times a day has three failure modes that
matter more than anything else:

1. **It gets boring.** Combinatorial generation drifts toward its favourite
   options and the feed starts looking like one video posted repeatedly.
2. **It gets expensive.** A loop calling a paid API is one config typo away from
   an unpleasant invoice.
3. **It fails at 3am.** A provider blips, a beat renders black, and either the
   whole night is lost or — worse — something broken gets published.

Nearly every design decision below is a response to one of those three.

---

## Layers

```
  cli.py                  argument parsing, human-readable output
    │
  pipeline.py             orchestration, stage sequencing, resumability
    │
    ├── ideas/            what to make          (pure, free, offline)
    ├── prompts/          how to ask for it     (pure, free, offline)
    ├── copywriting/      what to say about it  (pure, free, offline)
    ├── providers/        who makes it          (network, costs money)
    ├── media/            how to assemble it    (ffmpeg, CPU-bound)
    ├── scheduling/       when to post it
    └── publishing/       how to get it out
    │
  config.py  models.py  storage.py  budget.py  util.py
```

The top three feature packages are deliberately **pure and free**: they touch no
network and produce no files. You can generate ten thousand concepts and rank
them before spending anything. This is the single most useful property of the
design — iteration on the creative layer is instant.

---

## Data model

Three objects carry everything.

### `Concept`

The complete creative specification for one video: cast, theme, setting,
palette, lighting, effects, music, hook, payoff, loop strategy, and a list of
`Beat`s. It is JSON-serialisable, reproducible from its seed, and it is the only
input rendering needs.

```python
Concept(
    id="disco-night-kittens-puppies-a1b2c3d4",
    seed=20260806,
    cast=[CastMember(species="kitten", breed="orange tabby", ...)],
    beats=[Beat(index=1, role="hook", duration_s=3.2, camera="slow_push_in", ...)],
    ...
)
```

Concepts store **both labels and option ids**. The prose labels go into prompts;
the ids go into the novelty ledger. Deriving ids back from labels is lossy, and a
lossy key silently turns anti-repetition into a no-op — a bug this codebase
actually had.

### `Beat`

One shot: role, duration, action, camera move, transition, and the rendered
prompts. Beats are generated independently and stitched, which is what lets a
single failed beat be re-rendered without redoing the video.

### `JobRecord`

Runtime state: the concept, the copy, every file path, per-stage status, cost,
and the QC report. Serialised to `manifest.json` after **every stage**. That file
is the source of truth — not memory — which is what makes runs resumable and
every finished video auditable months later.

---

## Concept generation

### Constrained sampling (`ideas/vocabulary.py`)

Pure random mixing produces nonsense ("scuba corgi in a snowstorm wearing a Santa
hat"). Hand-authoring doesn't scale to seven a day forever. The middle path is a
**tag algebra**:

```yaml
- id: santa_hat
  tags: [holiday, headwear]
  requires: [christmas]     # gated on context
  excludes: [underwater]    # blocked by context
  weight: 1.0
```

Pools are sampled in a fixed order, each pick contributing its tags to a growing
context. Later pools only see compatible options. Result: an enormous space that
is always coherent.

Two details that matter in practice:

- **Fallback on over-constraint.** If constraints eliminate every option, the
  sampler relaxes rather than raising. A slightly over-tight config should
  degrade a video, not kill the night's batch.
- **Accessory slots.** Body slots (`headwear`, `eyewear`, `neckwear`, …) are
  enforced per performer, so no kitten wears two hats. It's derived from tags,
  so a new accessory joins the rule automatically.

### Beat planning (`ideas/structure.py`)

Length is drawn from a **duration band** (snap / short / standard / extended),
each with its own pacing. Roles are then assigned along the retention spine and
durations distributed by role weight with jitter.

The reconciliation between weights and limits is iterative, not single-pass:
clamping one beat frees or steals time from the others, and a single pass lets a
"capped" beat end up over its cap after redistribution.

**Beat count is derived from duration**, not sampled independently. Sampling both
produces 11-second shots that no current video model can generate. Deriving keeps
every beat near five seconds — one beat, one generation.

### Novelty (`ideas/novelty.py`)

A SQLite ledger with two mechanisms:

- **Signature rejection.** A concept whose option set is identical to, or >72%
  similar (Jaccard) to, a recent one is rejected and regenerated. This catches
  "same thing with one hat swapped", which is the failure mode that actually
  kills a faceless account.
- **Soft recency penalty.** Every recently-used option gets a weight multiplier
  below 1 that decays linearly back to 1.0 over ~60 videos. The sampler drifts
  toward under-used content rather than hard-banning anything.

Distance is measured in **videos**, via a per-channel `seq` counter. An earlier
version inferred it from SQLite rowids divided by an estimated options-per-video
figure; that estimate drifted as the library grew and quietly shortened the decay
window over time.

Concepts are committed to the ledger **one at a time during batch generation**,
not in bulk afterward. That's what makes a single day varied: video 1's penalty
is already steering the sampler when video 2 is drawn.

---

## Providers

Everything that costs money sits behind one of four `Protocol`s in
`providers/base.py`. The pipeline never imports a vendor SDK, so swapping
Replicate for fal.ai — or for a model that doesn't exist yet — is a config change
plus one new file.

Two policies do the heavy lifting:

**Cost is estimated before the call.** `estimate_cost()` lets the budget guard
refuse a spend *before* it happens, rather than reporting it after.

**Missing credentials degrade, they don't crash.** If a configured provider
raises `ProviderUnavailable` at construction time, the registry logs loudly and
substitutes `synth`. A forgotten key should cost you a night's quality, not a
night's output — and the batch report records exactly what was degraded.

The `synth` providers deserve their own note. They produce real, correctly-timed,
correctly-sized MP4s and on-tempo WAVs using ffmpeg and ~90 lines of pure-Python
synthesis. They make the whole system testable offline, in CI, with no secrets —
and they're the reason this codebase has end-to-end tests that render actual
video and probe the result.

---

## Media

`media/ffmpeg.py` builds **argument lists, never shell strings** — no quoting
bugs, no injection surface from a caption. Failures raise with the tail of
stderr, because ffmpeg's actual error is always in the last few lines.

### Camera motion (`media/motion.py`)

Two jobs: normalise whatever the provider returned to exactly 1080×1920 at the
project frame rate and exactly the beat's duration; then layer a deliberate
camera move on top. The second is the cheapest available upgrade to perceived
production value — generated clips tend to have static or floaty cameras, and a
directed move reads as intentional.

Normalisation scales to *cover* then centre-crops rather than pillarboxing,
because for a subject-centred cute-animal clip, keeping the face large is what
the format rewards.

### Assembly (`media/assemble.py`)

Hard cuts use the concat demuxer (stream copy, instant, lossless). Anything else
builds a single chained `xfade` graph — one graph is markedly faster and cleaner
than pairwise re-encoding.

The offset arithmetic has two rules, both learned by watching a 42-second video
come out at 20 seconds with ffmpeg exiting 0:

1. **The blend must be at least two frames long.** A sub-frame duration (0.04s at
   24fps) leaves `xfade` no frame pair to interpolate and the chain collapses.
   Hard cuts therefore use `2/fps`, not a hard-coded seconds value.
2. **The blend must end before the first input's last frame.** The offset carries
   one frame of headroom.

Because transitions consume time from both sides, the finished video is slightly
shorter than the sum of its beats. That's expected, and it's why QC uses a
tolerance and why subtitle timings are rescaled against the measured duration.

### Loop closing

Rewatches are the cheapest views on the platform. Three strategies:
`crossfade_to_start` (dissolve the tail into a copy of the opening — nearly
invisible at speed), `freeze_hold` (when the payoff is a pose worth reading), and
`hard_cut` (when the payoff *is* the end).

### QC (`media/qc.py`)

The gate between "rendered" and "publishable". Errors block; warnings log. Checks
duration drift, resolution, aspect, audio presence, file size, and black frames
via `blackdetect` — the classic silent generation failure. Publishing a black
video costs more than skipping the slot.

---

## Reliability

| Mechanism | Where | What it prevents |
|---|---|---|
| Per-stage manifest | `pipeline.py` | Re-rendering (and re-paying for) completed work |
| Freshness checks | `media/assemble.py` | Redoing ffmpeg passes whose output already exists |
| Per-job exception isolation | `pipeline._safe_build` | One broken video killing the batch |
| Retry with backoff | `util.retry` | Transient provider failures |
| Provider fallback | `providers/registry.py` | A missing key producing nothing |
| Budget guard | `budget.py` | A runaway loop and a surprise invoice |
| QC gate | `media/qc.py` | Publishing something broken |
| Atomic JSON writes | `util.write_json` | A reader seeing half a manifest |
| Caption/hook locks | `pipeline.py` | Parallel jobs picking identical copy |

Concurrency is a `ThreadPoolExecutor` over jobs. The work is dominated by
subprocess and network waits, so threads are the right tool; the only shared
mutable state is the caption/hook set, which is explicitly locked.

---

## Extending

| To add… | Do this | Code changes |
|---|---|---|
| Themes, costumes, dances, music | Append to the channel YAML | None |
| A new channel | One YAML file with `extends:` | None |
| A new prompt style | Edit `prompts/templates/*.j2`, or drop overrides in `config/prompt_overrides/<channel>/` | None |
| A new video model | One file in `providers/video/`, one line in `registry.py` | Two files |
| A new camera move | One function + one entry in `motion.py`, one entry in the channel YAML | One file |
| A new transition | One entry in `assemble.TRANSITIONS` (any `xfade` name), one in the YAML | One file |
| A new pipeline stage | One method, one entry in `Stage` and the stage list | One file |

Tests enforce the coupling that YAML can't: every `camera_moves` id must have an
implementation in `motion.py`, and every `transitions` id must map to a real
`xfade` name. A typo in a config file fails the suite instead of failing at 3am.
