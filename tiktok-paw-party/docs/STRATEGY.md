# Content strategy

The reasoning behind the pipeline's creative defaults, and what to change once
you have your own data.

A caveat worth stating up front: the specific numbers below (2-second hook,
seven posts a day, 12-second subtitle threshold) are **defensible starting
points, not laws**. They're encoded in config precisely so you can overturn them
with your own analytics. Nobody outside the platform knows the ranking function;
what follows is reasoning from mechanics that are observable.

---

## The one thing that matters

Short-form ranking is dominated by **watch time relative to length**, with
engagement as a multiplier. Two consequences drive everything else:

1. **A shorter video that gets watched twice beats a longer one abandoned at 40%.**
   Hence the loop work, and hence why the shortest duration band carries the
   heaviest weight.
2. **The first two seconds decide the rest.** Most viewers who leave, leave
   there. Everything after is optimising a smaller number.

---

## The hook (0–2s)

Encoded in `ideas/structure.py` and the `hooks` pool.

**Rules the pipeline enforces:**

- The first beat opens **mid-motion**. No fade in, no empty stage, no logo. The
  prompt template says so explicitly for `role: hook`, and the assembly stage
  never puts a transition before beat one.
- The hook beat is **capped at 4 seconds** (`ROLE_LIMITS["hook"]`). A hook that
  outstays its welcome is just a slow intro.
- On-screen hook text appears **at frame zero** and is gone within ~2.6s.
- The subject is centred with headroom, described in every prompt, because
  vertical crops punish off-centre composition.

**Why on-screen text at all**, on a video with no dialogue: most feed viewing
starts muted. A three-word card is the only thing carrying your promise for the
first second.

The hook cards are deliberately blunt — `WAIT FOR IT`, `RATE THIS 1-10`,
`WHO DID IT BETTER?`. They're not clever. Clever costs a beat of comprehension,
and you don't have one.

---

## Retention through the middle

The `switch` beat is placed at roughly 60–70% of the video, which is where
mid-video drop-off tends to concentrate. It's specified as a genuine pattern
interrupt — new rhythm, new angle, visible surprise — not just another shot.

The mid-video subtitle card lands on that same beat, for the same reason.

Each `build` beat is required to add a new visual fact: a costume, an angle, a
dancer. A beat that repeats the previous one is dead air, and dead air in the
middle is where a video loses its second half.

---

## The loop

Rewatches are the cheapest views available: the viewer who doesn't notice the
restart watches twice, and the second view costs nothing.

Three strategies, chosen per concept:

| Strategy | When it's right |
|---|---|
| `crossfade_to_start` | Default. Dissolves the tail into a copy of the opening frames — nearly invisible at speed, works on almost any footage. |
| `freeze_hold` | When the payoff is a pose worth reading for half a beat. |
| `hard_cut` | When the payoff *is* the ending and a loop would undercut it. |

The final beat also inherits beat one's camera move, so the restart doesn't jump.

---

## Length

Four bands rather than one fixed duration:

| Band | Length | Beats | Weight | Rationale |
|---|---|---|---|---|
| Snap | 9–13s | 2–3 | 0.28 | Maximum loop rate. One idea, one punchline. |
| Short | 14–21s | 3–4 | 0.32 | Hook + two escalations + payoff. The workhorse. |
| Standard | 22–32s | 4–6 | 0.26 | Room for a real switch beat and a costume change. |
| Extended | 33–45s | 6–8 | 0.14 | Mini-narrative. Only when the concept has stages. |

Mixing lengths keeps the account's average-view-duration signal healthy across
formats, and gives you the data to find out which band your audience actually
rewards. After a few weeks, check retention by band and re-weight in
`config/channels/<name>.yaml`.

---

## Engagement

Comments are the strongest engagement signal you can manufacture without gimmicks
— and questions manufacture comments.

The caption ranker in `copywriting/captions.py` scores structures, not
cleverness:

| Structure | Score | Example |
|---|---|---|
| Question | +3.0 | "Which one wins?" |
| Rating prompt | +2.5 | "Rate this from 1-10" |
| Comparison | +2.0 | "Who did it better?" |
| Imperative | +1.5 | "Name a better duo" |
| Superlative | +1.2 | "The cutest thing today" |
| 25–70 characters | +2.0 | reads fully in-feed without truncation |
| 1–2 emoji | +1.0 | more reads as spam |

Two anti-monotony rules matter as much as the scoring:

- **No two videos in a batch share a caption opening.** Ranking alone is
  deterministic, so the top-scoring pattern would win all seven slots.
- **No two videos in a batch share an on-screen hook card.**

Each video also ships a **pinned-comment seed** (`comment_seed`) in its export
bundle. Pinning a question as the first comment reliably outperforms burying it
in the caption.

**Duos over solos.** Cast size is weighted toward two performers (0.50 vs 0.28
solo) because "which one?" only works when there are two.

---

## Hashtags

Tiered rather than random, capped at eight:

| Tier | Count | Job |
|---|---|---|
| **core** | 2–3, constant | Tells the algorithm what the *account* is. Consistency here builds a stable audience profile over months. Change rarely. |
| **content** | varies | Derived from this video: species, dance, theme, setting, occasion. |
| **rotating** | 1 | Operator-maintained. Trends you actually verified. |
| **broad** | 2 | High-volume discovery. Useful, but where you compete with everyone. |

Species-specific tags are filtered against the cast, so a dogs-only video never
carries `#catsoftiktok`. Pulling the wrong audience is worse than no tag: they
scroll, and that scroll is a negative signal.

**Trending tags are deliberately not auto-fetched.** TikTok has no public
trending-hashtag API, and scraping one would put a fragile, ToS-risky dependency
inside a job that has to run unattended every day. Check the app's Creative Center
weekly and update the `rotating` list by hand — it takes two minutes.

Long hashtag walls dilute relevance and read as spam. Eight is the cap for a
reason.

---

## Posting schedule

Seven slots, spread across the day, with ±12 minutes of jitter.

**Why spread:** seven posts in one hour compete with each other for the same
audience.

**Why jitter:** posting at exactly 07:15:00 every day is a bot fingerprint, and
it also means you never learn whether 07:40 works better.

**Slot ordering:** the strongest video goes in the strongest slot. "Strongest" is
approximated by the caption's engagement structure with short videos breaking
ties (they loop more).

The default times are US-audience-shaped guesses. **Replace them within two weeks
of launch** with your own analytics — this is the single highest-value setting to
tune, and the only one where your data beats any general advice:

```yaml
schedule:
  post_times: ["07:15", "09:30", "12:00", "15:30", "18:00", "20:15", "22:00"]
  timezone: America/Los_Angeles
```

---

## Not repeating yourself

For a faceless account, this is the whole game. The novelty system is documented
in [ARCHITECTURE.md](ARCHITECTURE.md); the operational summary:

- Near-duplicates (>72% option overlap) are rejected and regenerated.
- Recently used options are down-weighted, decaying over ~60 videos.
- Each concept is committed before the next is drawn, so a single day varies.
- `pawparty stats` shows the most-used options. **A runaway entry there is your
  early warning** that the vocabulary needs widening.

At seven a day you'll consume about 2,500 videos a year. The default vocabulary
has an effective space in the billions, so exhaustion isn't the risk —
*clustering* is. Watch the leaderboard, and add options in the categories that
dominate it.

---

## What to measure

After two weeks, pull from your analytics and compare against the manifests:

| Metric | Where the lever is |
|---|---|
| Watch time by **duration band** | `duration_bands` weights |
| Drop-off timestamp | Hook length, `switch` beat placement |
| Rewatch rate | `loop_strategies` weights |
| Comments per view by **caption structure** | `caption_patterns`, ranker weights |
| Views by **post time** | `schedule.post_times` |
| Views by **theme** | `themes` weights |

Every one of those is a config edit, not a code change. That's the point of the
split.

---

## Things this pipeline deliberately does not do

- **Engagement bait that lies** ("wait for the ending" when there is no ending).
  It works once and costs you the follow.
- **Follow-for-follow tags.** Banned in the default config.
- **Trend scraping.** Fragile and ToS-risky in an unattended job.
- **Hiding that it's AI.** Every export bundle carries the disclosure checklist,
  and the API client sets the AI-generated flag by default. Mislabelling risks
  the account, not just the post. See [LEGAL.md](LEGAL.md).
