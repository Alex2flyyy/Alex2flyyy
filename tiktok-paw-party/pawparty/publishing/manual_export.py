"""Manual publishing export — the path that works for everyone, today.

TikTok's Content Posting API requires an approved developer app, and approval is
neither instant nor guaranteed (see docs/PUBLISHING.md). Until you have it — and
as a permanent fallback if you never do — this exporter produces a folder you
can post from in about thirty seconds per video:

    Videos/Scheduled/2026-08-06/upload/
      01_0715_disco-kittens/
        final.mp4          ← drag into the TikTok app or web uploader
        POST.txt           ← caption + hashtags, ready to paste
        cover.jpg
        details.md         ← post time, alternates, pinned comment

Nothing here talks to any network.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path
from typing import Sequence

from ..logging_setup import get_logger
from ..scheduling.scheduler import Slot
from ..storage import Workspace
from ..util import today_stamp, write_text

log = get_logger("publishing.manual")


def export(
    slots: Sequence[Slot],
    workspace: Workspace,
    *,
    date: _dt.date | str | None = None,
) -> Path:
    """Write a post-ready folder per slot. Returns the parent directory."""
    stamp = date if isinstance(date, str) else today_stamp(date)
    root = workspace.scheduled / stamp / "upload"
    root.mkdir(parents=True, exist_ok=True)

    for slot in slots:
        video = Path(slot.video_path)
        if not video.exists():
            log.warning("slot %d has no video at %s; skipping", slot.index, video)
            continue

        time_part = slot.post_at_local[11:16].replace(":", "")
        folder = root / f"{slot.index:02d}_{time_part}_{slot.concept_id}"
        folder.mkdir(parents=True, exist_ok=True)

        shutil.copy2(video, folder / "final.mp4")
        if slot.cover_path and Path(slot.cover_path).exists():
            shutil.copy2(slot.cover_path, folder / "cover.jpg")

        hashtags = " ".join(f"#{t}" for t in slot.hashtags)
        write_text(folder / "POST.txt", f"{slot.caption}\n\n{hashtags}\n")
        write_text(folder / "details.md", _details(slot, hashtags))

    write_text(root / "README.md", _index(slots, stamp))
    log.info("manual upload bundle written to %s", root)
    return root


def _details(slot: Slot, hashtags: str) -> str:
    alternates = slot.metadata.get("alternate_captions") or []
    lines = [
        f"# {slot.title}",
        "",
        f"- **Post at:** {slot.post_at_local}  (UTC: {slot.post_at_utc})",
        f"- **Length:** {slot.duration_s:.0f}s",
        f"- **Theme:** {slot.metadata.get('theme', '—')}",
        f"- **Music:** {slot.metadata.get('music', '—')}",
        "",
        "## Caption",
        "",
        f"{slot.caption}",
        "",
        "## Hashtags",
        "",
        hashtags,
        "",
        "## Pin this as the first comment",
        "",
        slot.metadata.get("comment_seed", "—"),
        "",
        "## Alternate captions",
        "",
    ]
    lines += [f"- {c}" for c in alternates] or ["- (none)"]
    lines += [
        "",
        "## Accessibility",
        "",
        slot.metadata.get("alt_text", "—"),
        "",
        "## Before posting",
        "",
        "- [ ] Toggle **AI-generated content** on in the TikTok post screen.",
        "- [ ] Confirm the music is cleared for your account type (see docs/LEGAL.md).",
        "- [ ] Watch the first 2 seconds once — the hook has to land.",
        "",
    ]
    return "\n".join(lines)


def _index(slots: Sequence[Slot], stamp: str) -> str:
    lines = [
        f"# Upload queue — {stamp}",
        "",
        f"{len(slots)} video(s). Post in order; each folder has its own POST.txt.",
        "",
        "| # | Local time | Title | Length | Folder |",
        "|---|-----------|-------|--------|--------|",
    ]
    for slot in slots:
        time_part = slot.post_at_local[11:16]
        folder = f"{slot.index:02d}_{time_part.replace(':', '')}_{slot.concept_id}"
        lines.append(
            f"| {slot.index} | {time_part} | {slot.title} | "
            f"{slot.duration_s:.0f}s | `{folder}` |"
        )
    lines += [
        "",
        "> Every video must be labelled as AI-generated in the TikTok post screen.",
        "",
    ]
    return "\n".join(lines)
