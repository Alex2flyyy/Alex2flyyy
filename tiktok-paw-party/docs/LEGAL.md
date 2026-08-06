# Legal and policy notes

Not legal advice — I'm describing how the system is built and where the real
risks sit. For anything with money attached, talk to someone qualified in your
jurisdiction.

The short version: the two things most likely to cost you an account are **music
licensing** and **AI disclosure**, in that order.

---

## Music

### The generated bed is not for publishing

The `synth` music provider writes a pure-Python bed at the right tempo and length
so you can validate the mix, the ducking, the fades and the cut timing. It is a
development tool. Do not publish with it — not because of a licensing problem
(it's original code output), but because it sounds like what it is.

More importantly, **generative music from third-party models is legally murky for
monetised social accounts.** Terms vary widely on commercial use and on whether
the output is licensable at all, and platform audio detection can mute or restrict
content whose audio it can't clear. For an account you intend to grow, this is not
a good place to save money.

### Use licensed library music

The boring, safe answer:

| Source | Notes |
|---|---|
| **TikTok Commercial Music Library** | Free, pre-cleared for the platform. Only available when adding sound in the app, so it doesn't fit the render-then-upload flow — but it's an option if you post manually. |
| **Epidemic Sound / Artlist / Soundstripe** | Subscription. Broad catalogues, clear commercial terms, downloadable files that fit this pipeline. |
| **Musicbed, Artlist Pro** | Higher-end, per-track or subscription. |
| **CC0 / public domain** | Free, but verify each track individually. "Free download" is not a licence. |

Setup:

```
Videos/Audio/library/
├── library.yaml
└── your_licensed_tracks.mp3
```

```yaml
providers:
  music: local_library
```

### Record your licences

`library.yaml` has a `license` field. The code doesn't read it. It exists so that
in eighteen months, when a claim lands or a subscription lapses, you can answer
"where did this track come from?" without guessing:

```yaml
tracks:
  - file: disco_paws.mp3
    license: "Epidemic Sound — subscription #12345, downloaded 2026-08-01"
```

**Subscription licences usually cover only the period you were subscribed.**
Cancelling can mean previously published videos lose their coverage. Check your
provider's terms on that specifically — it's the detail people get caught by.

### Popular music

Using a chart track because "TikTok has it in the app" is the most common way
faceless accounts get muted or struck. In-app sounds are licensed for *personal*
use on the platform; a monetised faceless content operation is not that. If your
account earns anything, the calculation changes and not in your favour.

---

## AI disclosure

**Every video must be labelled as AI-generated.**

TikTok's synthetic media policy requires disclosure for realistic AI-generated
content. YouTube and Instagram have their own equivalents. Detection is decent
and improving, and undisclosed AI content is a policy violation against the
account, not just the post.

The pipeline handles this in both directions:

- The API client sets `is_aigc=True` by default.
- Every exported `details.md` puts the toggle on a pre-post checklist.

### Don't imply the animals are real

The channel config bans "real" and "rescued" from captions, and generated alt
text always begins "AI-animated". This isn't squeamishness — presenting synthetic
animals as a real rescue story is the category of thing that gets accounts
removed and, depending on where you are and whether money is involved, can shade
into deceptive-practices territory.

Cute AI animals, clearly labelled: fine, and a large and growing category.

---

## Generated content ownership

Whether you own AI model output, and whether you can register copyright in it,
varies by jurisdiction and is genuinely unsettled. In the US, the Copyright Office
has held that purely AI-generated material isn't copyrightable without human
authorship.

Practically, for this project:

- **Check your provider's terms** on commercial use of outputs. Most current
  video model hosts permit it; some restrict it by plan tier.
- **You may not be able to stop someone reposting your videos.** Factor that into
  any business plan that depends on exclusivity.
- **The prompts, the vocabulary and this code are yours.** That's where the
  durable asset is, not in any individual clip.

---

## Platform terms

Automating content *creation* is fine. Automating *account actions* is where
platform terms get restrictive.

| Activity | Status |
|---|---|
| Generating and rendering videos offline | Fine |
| Uploading via the official Content Posting API | Fine, with an approved app |
| Uploading by hand | Fine |
| Scraping trending hashtags or sounds | **Against ToS.** Deliberately not implemented here. |
| Unofficial upload libraries / browser automation | **Against ToS.** Bannable. Not implemented. |
| Automated follows, likes, comments | **Against ToS.** Not implemented. |

This project implements exactly the first three. The gaps are intentional, and
`docs/STRATEGY.md` explains the trending-hashtag decision in more detail.

---

## Animal welfare framing

The subject is animals, so it's worth being deliberate. The negative prompts
exclude distressed, caged, injured, frightened and aggressive animals; the safety
config bans a list of words from captions. Everything the pipeline generates is
meant to read as wholesome.

The reason is partly brand and partly that "cute animal in visible distress"
performs well enough to be tempting, and it's a bad thing to build a machine for.

---

## Practical checklist before you launch

- [ ] Music is licensed, and the licence is recorded in `library.yaml`.
- [ ] `providers.music` is `local_library`, not `synth`.
- [ ] The AI-generated label is on, on every post.
- [ ] Your video model's terms permit commercial use on your plan.
- [ ] Captions don't claim the animals are real.
- [ ] `pawparty costs` caps match a number you're comfortable with.
- [ ] You've watched a full day's output end to end before automating it.
