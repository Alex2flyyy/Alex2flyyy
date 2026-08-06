# Publishing

Getting finished videos onto TikTok — what's automatable, what isn't, and why.

---

## Read this first

**Automated posting to TikTok is not something this project can give you.**

The Content Posting API requires a developer app that TikTok reviews and approves
manually. Approval is not guaranteed, has no published timeline, and depends on
your use case and account standing. Anyone claiming to hand you fully automated
TikTok posting is either using an unofficial (bannable) method or hasn't tried.

So there are two paths, and the manual one is not a consolation prize — it's what
most people running accounts like this actually use.

| Path | Setup | Effort per video | Reliability |
|---|---|---|---|
| **Manual export** | None | ~30 seconds | Always works |
| **Content Posting API** | Developer app + approval + OAuth | Zero | Subject to approval and token upkeep |

---

## Manual export

```bash
pawparty run --count 7 --schedule --export
# or, for an existing day:
pawparty publish --manual --date 2026-08-06
```

Produces:

```
Videos/Scheduled/2026-08-06/upload/
├── README.md                          # the day's queue as a table
├── 01_0715_disco-night-kittens-a1b2/
│   ├── final.mp4                      # drag this into TikTok
│   ├── POST.txt                       # caption + hashtags, ready to paste
│   ├── cover.jpg
│   └── details.md                     # post time, alternates, pinned comment,
│                                      # and the pre-post checklist
├── 02_0930_.../
└── ...
```

The routine per video: open the folder, drag `final.mp4` into the TikTok app or
web uploader, paste `POST.txt`, toggle the AI-generated label, post, then pin the
comment from `details.md`.

Seven videos is about three to four minutes a day. Realistically, you can also
post them in one sitting using TikTok's own scheduler (web uploader → *Schedule
video*), which gets you the timing benefits without the API.

---

## Content Posting API

### What you need

All five, and none of it can be provisioned by this code:

1. A **TikTok for Developers** account and a registered app
   (<https://developers.tiktok.com/>).
2. The **Content Posting API** product added to that app, **approved by TikTok**.
   Manual review.
3. Scopes:
   - `video.upload` — upload as a draft to the user's inbox. Granted more readily.
   - `video.publish` — post directly with no human tap. Granted more sparingly.
4. A completed **OAuth flow** producing an access token and refresh token for the
   target account. Access tokens are short-lived; refresh tokens also expire.
5. An **audited app** if you want posts to be public. Unaudited apps are
   restricted to private/self-view content — which is why the default privacy
   level in this client is `SELF_ONLY`.

### Two modes

```yaml
# inbox (default): lands as a draft; the creator taps to finish posting.
# Needs only video.upload. This is the realistic option for most people.
pawparty publish --mode inbox

# direct: posts without intervention. Needs video.publish.
pawparty publish --mode direct --privacy PUBLIC_TO_EVERYONE
```

### Configuration

```bash
# .env
TIKTOK_CLIENT_KEY=...
TIKTOK_CLIENT_SECRET=...
TIKTOK_ACCESS_TOKEN=...
TIKTOK_REFRESH_TOKEN=...
```

```bash
pawparty publish --date 2026-08-06 --due-only --dry-run   # inspect first
pawparty publish --date 2026-08-06 --due-only             # then for real
```

`--due-only` posts only slots whose scheduled time has arrived, within a
90-minute grace window. A slot missed by nine hours is left for you to
reschedule rather than dumped out at 3am on top of the next one.

If `TIKTOK_ACCESS_TOKEN` is unset, `pawparty publish` says so plainly and writes
a manual bundle instead. It never silently does nothing.

### Token refresh

Access tokens expire in hours. `TikTokPublisher.refresh()` exchanges the refresh
token for a new one, but **refresh tokens expire too** (typically a year), and
when that happens the OAuth flow has to be repeated by a human. Any long-running
automation needs a monitor for this — a publish job that has been quietly failing
for a week is a bad way to find out.

### Implementation notes

`pawparty/publishing/tiktok_api.py` targets the v2 endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /v2/post/publish/inbox/video/init/` | Start a draft upload |
| `POST /v2/post/publish/video/init/` | Start a direct post |
| `POST /v2/post/publish/status/fetch/` | Poll until complete |
| `POST /v2/post/publish/creator_info/query/` | Posting limits and privacy options |
| `POST /v2/oauth/token/` | Refresh |

Uploads are single-chunk (the API allows it below 64MB; our videos are ~15MB).
Files above that raise a clear error rather than silently failing — if you hit
it, lower render quality or add chunked upload.

The API changes. Request shapes are kept in one module so an update is a small,
contained edit.

---

## AI content labelling

**Every video from this pipeline must be labelled as AI-generated.**

TikTok's synthetic media policy requires it for realistic AI-generated content.
Failing to label risks the account, not just the post — and the platform's own
detection is reasonably good at this.

- **API:** `is_ai_generated=True` is the default in `TikTokPublisher.publish()`,
  which sets `is_aigc` in the post payload.
- **Manual:** the toggle is in the TikTok post screen under *More options → AI-
  generated content*. It's on the checklist in every exported `details.md`.

Related, and worth a moment's thought: these videos should never be presented as
real animals. The pipeline's alt text always says "AI-animated", and the channel
config bans "real" and "rescued" from captions. Cute AI animals are fine.
Cute AI animals passed off as a real rescue story is a different thing, and it's
the kind of thing that gets accounts removed.

---

## Cross-posting

The same 1080×1920 files work on YouTube Shorts and Instagram Reels, and each
finished bundle includes an SRT sidecar (`Videos/Captions/<date>/<id>/subs.srt`)
that YouTube can ingest — TikTok ignores it, which is why the on-screen text is
burned in as well.

Both platforms have their own AI-disclosure requirements. Check them before
cross-posting; they are not identical to TikTok's.
