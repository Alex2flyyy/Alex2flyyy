# Providers

Going from free previews to real AI-generated dancing animals.

---

## The short version

Out of the box, `providers.video` is `synth` — an offline placeholder that builds
real MP4s with ffmpeg so the whole pipeline runs without credentials. It does not
produce animals. To get actual footage you need an account with a video-model
host and two edits to `config/settings.yaml`.

```yaml
providers:
  video: replicate          # was: synth
  options:
    replicate:
      model: "owner/model-name"
      cost_per_second_usd: 0.05
```

Plus `REPLICATE_API_TOKEN` in `.env`. That's it — everything downstream is
unchanged.

---

## Why no model is hard-coded

This project deliberately ships **no default model slug**. The best available
video model changes every few months, pricing changes with it, and a pinned slug
in a repo like this rots into a broken default that fails confusingly. Instead
the integration is generic and you point it at whatever is current.

That normally means reading your chosen model's input schema and hand-mapping
its field names. `pawparty wire` does that for you — see below.

---

## `pawparty wire` — the fast path

```bash
export REPLICATE_API_TOKEN=r8_...          # or put it in .env
pawparty wire owner/model-name             # inspect
pawparty wire owner/model-name --apply     # write the config
```

It fetches the model's real input schema, works out which of its fields
correspond to PawParty's, and prints the config — flagging anything it could
not resolve:

```
acme/dancer-v2  (replicate)
  inputs: aspect_ratio, duration, motion_strength, negative_prompt, prompt, seed, start_image

proposed config
------------------------------------------------------------
providers:
  video: replicate
  options:
    replicate:
      model: "acme/dancer-v2"
      cost_per_second_usd: 0.05   # ← set from the model's pricing page
      input_map:
        prompt: prompt
        negative_prompt: negative_prompt
        duration: duration
        seed: seed
        first_frame: start_image
      static_input:
        aspect_ratio: "9:16"
        motion_strength: "TODO"
------------------------------------------------------------
  note: duration only accepts [5, 10]. PawParty requests the nearest value
        and trims to the exact beat length — trimming is free.
  WARNING: Required input(s) with no PawParty equivalent: motion_strength.
```

It is **advisory on purpose**. It tells you what it worked out and what it
didn't, rather than guessing confidently — a wrong `input_map` fails at
generation time, after a batch has already started spending. Two things it
always leaves to you:

- **`cost_per_second_usd`** — it cannot read pricing. The budget guard is only
  as accurate as this number, so set it from the model's pricing page.
- **`TODO` placeholders** for required inputs it has no equivalent for.
  `pawparty doctor` fails while any remain, so you can't accidentally run with
  one.

Add `--show-schema` to dump every input the model takes, and `--kind image` to
wire an image model into the stills slot.

### Where `--apply` writes

`config/settings.local.yaml` — a git-ignored overlay deep-merged over
`settings.yaml`. Machine-specific choices (your model, your budget, your posting
times) live there, so you can pull updates to the shared config without a merge
conflict every time. Delete the file to revert to defaults.

---

## Choosing a model

Browse:

- Replicate — <https://replicate.com/collections/text-to-video>
- fal.ai — <https://fal.ai/models?categories=text-to-video>

What matters for this use case, roughly in order:

| Criterion | Why it matters here |
|---|---|
| **Vertical / 9:16 support** | The whole format. A model that only does 16:9 costs you a crop and the animal's face. |
| **Motion quality** | These are dance videos. Stuttering or morphing limbs is fatal; a static-but-pretty model is useless. |
| **Animal anatomy** | Many models are trained heavily on humans and produce five-legged puppies. Test this first. |
| **Image-to-video support** | Big deal — see below. |
| **Clip length** | 5–10s covers our beats. Shorter than 4s forces awkward retiming. |
| **Cost per second** | At ~35 clips/day this dominates everything else. |

### Test before you commit

Generate a handful of clips by hand on the provider's own site with one of
PawParty's prompts before wiring anything up:

```bash
pawparty ideas --count 1 --save
cat Videos/Prompts/*/*/prompts.md
```

Paste a beat prompt in, look at the result. Five minutes here saves a lot of
money.

### A note on output resolution

PawParty renders to 1080×1920, but many video models top out at 720×1280.
That is not a blocker — the normalisation stage upscales — but upscaled
generation is softer than native, and the platform's own re-encode is
unforgiving of soft footage.

Three options, in order of preference:

1. **Use a model that outputs 1080p natively**, if one is available at a price
   you like. Check its `resolution` or `width`/`height` inputs.
2. **Accept the upscale.** At phone size, on bright saturated content, it is
   less visible than you'd expect. This is a reasonable default.
3. **Lower `render.width`/`render.height` to 720×1280.** Still 9:16, still
   accepted everywhere, and avoids upscaling entirely. Update `qc.expect_width`
   and `qc.expect_height` to match or QC will reject every video.

---

## Text-to-video vs image-to-video

**Prefer image-to-video where you can.**

Text-to-video generates each beat independently, so the orange tabby in beat 1
and the orange tabby in beat 3 are different cats. On a multi-beat dance video
that reads as a continuity error.

Image-to-video generates a still first, then animates it. PawParty's `stills`
stage does this automatically whenever your video provider's `input_map` includes
a `first_frame` key — one still per beat, seeded from the concept, keeping the
character far more consistent.

The tradeoff is one extra generation per beat (cheap — images cost cents) and a
bit more wall-clock time.

```yaml
providers:
  video: replicate
  image: replicate          # ← turn on the stills stage
  options:
    replicate:
      input_map:
        first_frame: image  # ← the model's own name for its start-image input
    replicate_image:
      model: "owner/some-image-model"
```

To use text-to-video instead, simply omit `first_frame` from `input_map`. The
stills stage skips itself.

---

## Configuring Replicate

```yaml
providers:
  video: replicate
  options:
    replicate:
      # Either "owner/model" (latest version) or "owner/model:versionhash" (pinned).
      # Pinning is safer for an unattended pipeline — a model update can change
      # output style overnight.
      model: "owner/model-name:abc123..."

      # From the model's pricing page. Used by the budget guard, so a wrong
      # value here makes your caps meaningless.
      cost_per_second_usd: 0.05
      timeout_s: 900

      # Map PawParty's fields onto the model's input names. Check the model's
      # "API" tab for the real names — they vary a lot.
      input_map:
        prompt: prompt
        negative_prompt: negative_prompt   # omit if unsupported
        duration: duration                 # omit if the model has a fixed length
        seed: seed
        first_frame: image                 # omit for text-to-video

      # Anything else the model wants, passed through untouched.
      static_input:
        aspect_ratio: "9:16"
        fps: 24
```

Set `REPLICATE_API_TOKEN` in `.env`
(<https://replicate.com/account/api-tokens>).

**Fixed-length models.** Many only accept discrete durations (5s, 10s). PawParty
requests the nearest supported value and trims to the exact beat length during
assembly — trimming is free, regenerating is not.

---

## Configuring fal.ai

Same shape, different names:

```yaml
providers:
  video: fal
  options:
    fal:
      model: "fal-ai/<model-id>"
      cost_per_second_usd: 0.06
      input_map:
        prompt: prompt
        duration: duration
        seed: seed
        first_frame: image_url
      static_input:
        aspect_ratio: "9:16"
```

Set `FAL_KEY` in `.env` (<https://fal.ai/dashboard/keys>).

The client uses fal's **queue** API rather than the synchronous one, because
video jobs routinely exceed any sane HTTP timeout.

---

## Music

| Provider | Use when |
|---|---|
| `synth` | Development and testing. A pure-Python synth generates an on-tempo, correct-length bed so you can validate the mix. **Not for publishing.** |
| `local_library` | **Production.** Your own licensed tracks. |

Setting up the library:

```
Videos/Audio/library/
├── library.yaml
├── disco_paws.mp3
└── beach_bounce.wav
```

```yaml
# Videos/Audio/library/library.yaml
tracks:
  - file: disco_paws.mp3
    label: "Disco Paws"
    bpm: 118
    energy: high
    tags: [disco, retro, party, high]
    license: "Epidemic Sound — subscription #12345"
  - file: beach_bounce.wav
    bpm: 104
    energy: medium
    tags: [tropical, summer, beach]
    license: "Artlist — invoice 9981"
```

```yaml
providers:
  music: local_library
```

Tracks are scored against the concept's vibe tags and BPM, then chosen from the
top matches with a seeded shuffle — so a good match gets used often, but not the
same track twice in a row when alternatives exist. Files without a `library.yaml`
entry still work; they just can't be matched on vibe.

The `license` field isn't used by the code. It's there so that in eighteen months
you can answer "where did this track come from?" without guessing. See
[LEGAL.md](LEGAL.md).

---

## LLM copywriting

Optional. The offline template writer produces ranked, deterministic, free
captions and handles diversity across a batch — it is genuinely fine.

Adding an LLM buys more variety in phrasing, for well under a cent per video:

```yaml
providers:
  llm: anthropic
  options:
    anthropic:
      model: claude-sonnet-5
      max_tokens: 900
      temperature: 1.0
```

Set `ANTHROPIC_API_KEY` in `.env`. LLM captions are merged with the template
captions and ranked together, so the model competes rather than overrides. **Any
LLM failure falls back to templates silently** — an API blip must never cost you
a post.

The prompt lives in `pawparty/prompts/templates/copy_llm.j2` and is yours to
edit.

---

## Writing a new provider

Four steps.

**1. Implement the Protocol** (`providers/base.py` has all four):

```python
# pawparty/providers/video/mymodel.py
from ..base import BaseProvider, GenerationResult, ProviderUnavailable

class MyModelProvider(BaseProvider):
    name = "mymodel"

    def __init__(self, options=None):
        super().__init__(options)
        self.key = os.getenv("MYMODEL_API_KEY", "")

    def _check(self):
        """Called at construction. Raise to degrade to synth instead of crashing."""
        if not self.key:
            raise ProviderUnavailable("MYMODEL_API_KEY is not set.")

    def estimate_cost(self, duration_s: float) -> float:
        return 0.04 * duration_s

    def generate_clip(self, *, prompt, negative_prompt, duration_s, width, height,
                      fps, output, seed, first_frame=None) -> GenerationResult:
        ...  # write the file to `output`
        return GenerationResult(path=output, cost_usd=self.estimate_cost(duration_s),
                                provider=self.name, model="my-model-v2")
```

**2. Register it** in `providers/registry.py`:

```python
VIDEO_PROVIDERS = {..., "mymodel": MyModelProvider}
```

**3. Select it** in `config/settings.yaml`.

**4. Reuse the HTTP helpers** in `providers/http.py` — `poll_until` (bounded
polling with backoff), `download` (streamed, so a 40MB video never sits in RAM),
and `first_url` (finds the output URL in whatever shape the response arrives in).

Three things to get right, because they're what separate a provider that survives
unattended operation from one that doesn't:

- Raise `ProviderUnavailable` (not `ProviderError`) for missing configuration, so
  the registry degrades instead of failing the batch.
- Make `estimate_cost()` honest. It's what the budget guard acts on.
- Never leave a partial file at `output` on failure. Download to a temp path and
  rename, as `providers/http.download` does — the pipeline treats an existing
  output file as completed work.

---

## Verifying a switch

```bash
pawparty doctor                       # confirms keys and provider selection
pawparty run --count 1 -v             # one video, verbose
```

Then check `Videos/Logs/run-<date>.jsonl` for the recorded provider and cost, and
watch the finished MP4. If concepts look right but footage doesn't, the problem
is the model or the prompts, not the pipeline — the exact prompt sent is archived
in `Videos/Prompts/<date>/<concept-id>/prompts.md`.
