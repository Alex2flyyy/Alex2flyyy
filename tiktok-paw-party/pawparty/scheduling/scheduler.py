"""Posting schedule.

Turns a day's finished videos into an ordered queue of slots. Two decisions are
baked in and worth stating:

* **Spread, don't burst.** Seven posts in an hour compete with each other for
  the same audience. Slots are spread across the day, with the highest-scoring
  video placed in the strongest slot.
* **Jitter.** Posting at exactly 07:15:00 every single day is a bot fingerprint
  and it also means you never learn whether 07:40 works better. Each slot gets
  a random offset within ``schedule.jitter_minutes``.

Slot times are *local wall-clock* in ``schedule.timezone``. Stored timestamps
carry the UTC offset so a cron running in UTC does the right thing.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..config import ScheduleSettings
from ..logging_setup import get_logger
from ..models import JobRecord
from ..storage import Workspace
from ..util import iso, read_json, seeded_rng, today_stamp, write_json

log = get_logger("scheduling")

try:  # Python 3.9+ stdlib; the fallback keeps this importable on odd builds
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


@dataclass
class Slot:
    """One planned post."""

    index: int
    concept_id: str
    title: str
    post_at_local: str
    post_at_utc: str
    video_path: str
    caption: str
    hashtags: list[str]
    cover_path: str = ""
    duration_s: float = 0.0
    status: str = "scheduled"       # scheduled | published | failed | skipped
    published_at: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    """Plans, persists and updates the daily posting queue."""

    def __init__(self, settings: ScheduleSettings, workspace: Workspace) -> None:
        self.settings = settings
        self.workspace = workspace

    # ------------------------------------------------------------------ #
    def _tz(self) -> _dt.tzinfo:
        if ZoneInfo is None:
            return _dt.timezone.utc
        try:
            return ZoneInfo(self.settings.timezone)
        except Exception:
            log.warning("unknown timezone %r; falling back to UTC", self.settings.timezone)
            return _dt.timezone.utc

    def queue_path(self, date: _dt.date | str | None = None) -> Path:
        stamp = date if isinstance(date, str) else today_stamp(date)
        return self.workspace.scheduled / stamp / "queue.json"

    # ------------------------------------------------------------------ #
    def plan(
        self,
        jobs: Sequence[JobRecord],
        *,
        date: _dt.date | None = None,
        seed: int | None = None,
    ) -> list[Slot]:
        """Assign finished jobs to today's posting slots.

        Ordering: strongest video first into the strongest slot. "Strongest" is
        approximated by the caption's engagement score proxy — a question or a
        rating prompt outperforms a statement — with shorter videos breaking
        ties, since they loop more.
        """
        date = date or _dt.date.today()
        tz = self._tz()
        rng = seeded_rng(today_stamp(date), "schedule", seed or 0)

        publishable = [j for j in jobs if j.final_path and Path(j.final_path).exists()]
        if not publishable:
            log.warning("no publishable videos to schedule for %s", today_stamp(date))
            return []

        ranked = sorted(publishable, key=self._strength, reverse=True)
        times = self._slot_times(date, tz, rng, len(ranked))

        slots: list[Slot] = []
        for index, (job, when) in enumerate(zip(ranked, times), start=1):
            copy = job.copy
            slots.append(
                Slot(
                    index=index,
                    concept_id=job.concept.id,
                    title=job.concept.title,
                    post_at_local=when.isoformat(),
                    post_at_utc=when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    video_path=job.final_path,
                    caption=copy.chosen_caption if copy else job.concept.title,
                    hashtags=copy.hashtags if copy else [],
                    cover_path=job.cover_path,
                    duration_s=job.concept.duration_s,
                    metadata={
                        "theme": job.concept.theme,
                        "occasion": job.concept.occasion,
                        "music": job.concept.music.label,
                        "comment_seed": copy.comment_seed if copy else "",
                        "alt_text": copy.alt_text if copy else "",
                        "alternate_captions": copy.captions[1:] if copy else [],
                    },
                )
            )

        self.save(slots, date=date)
        log.info("scheduled %d post(s) for %s", len(slots), today_stamp(date))
        return slots

    def _slot_times(
        self,
        date: _dt.date,
        tz: _dt.tzinfo,
        rng,
        count: int,
    ) -> list[_dt.datetime]:
        """Build ``count`` jittered local datetimes from the configured times.

        If there are more videos than configured slots, extra slots are
        interpolated into the largest gaps rather than stacked at the end.
        """
        base = [self._parse_time(t) for t in self.settings.post_times]
        base = [t for t in base if t is not None]
        if not base:
            base = [_dt.time(9, 0), _dt.time(13, 0), _dt.time(18, 0)]

        chosen = list(base[:count])
        while len(chosen) < count:
            chosen.append(self._widest_gap_time(chosen))
            chosen.sort()

        out: list[_dt.datetime] = []
        for slot_time in sorted(chosen):
            offset = rng.uniform(-self.settings.jitter_minutes, self.settings.jitter_minutes)
            when = _dt.datetime.combine(date, slot_time, tzinfo=tz) + _dt.timedelta(minutes=offset)
            out.append(when.replace(second=0, microsecond=0))
        return out

    @staticmethod
    def _parse_time(value: str) -> _dt.time | None:
        try:
            hour, _, minute = str(value).partition(":")
            return _dt.time(int(hour), int(minute or 0))
        except (TypeError, ValueError):
            log.warning("ignoring malformed post time %r (expected HH:MM)", value)
            return None

    @staticmethod
    def _widest_gap_time(times: list[_dt.time]) -> _dt.time:
        """Midpoint of the largest gap in the current slot list."""
        if len(times) < 2:
            return _dt.time(12, 0)
        minutes = sorted(t.hour * 60 + t.minute for t in times)
        best_gap, best_mid = -1, 12 * 60
        for a, b in zip(minutes, minutes[1:]):
            if b - a > best_gap:
                best_gap, best_mid = b - a, (a + b) // 2
        return _dt.time(best_mid // 60 % 24, best_mid % 60)

    @staticmethod
    def _strength(job: JobRecord) -> float:
        """Cheap proxy for expected performance, used only for slot ordering."""
        score = 0.0
        if job.copy:
            caption = job.copy.chosen_caption.lower()
            if "?" in caption:
                score += 3
            if any(word in caption for word in ("rate", "which", "who", "1-10")):
                score += 2
            if job.copy.onscreen_hook:
                score += 1
        # Shorter videos loop more; nudge them earlier in the day.
        score += max(0.0, (30 - job.concept.duration_s) / 30)
        return score

    # ------------------------------------------------------------------ #
    def save(self, slots: Sequence[Slot], *, date: _dt.date | None = None) -> Path:
        path = self.queue_path(date)
        write_json(
            path,
            {
                "date": today_stamp(date),
                "timezone": self.settings.timezone,
                "generated_at": iso(),
                "slots": [asdict(s) for s in slots],
            },
        )
        # One file per slot as well — easier to hand to an external scheduler
        # or to eyeball in a file browser.
        for slot in slots:
            stamp = slot.post_at_local[11:16].replace(":", "")
            write_json(path.parent / f"{stamp}_{slot.concept_id}.json", asdict(slot))
        return path

    def load(self, date: _dt.date | str | None = None) -> list[Slot]:
        path = self.queue_path(date)
        if not path.exists():
            return []
        data = read_json(path)
        return [Slot(**s) for s in data.get("slots", [])]

    def update_slot(self, slot: Slot, *, date: _dt.date | str | None = None) -> None:
        """Persist a status change (published / failed) back into the queue."""
        slots = self.load(date)
        for index, existing in enumerate(slots):
            if existing.concept_id == slot.concept_id:
                slots[index] = slot
                break
        stamp = date if isinstance(date, str) else today_stamp(date)
        self.save(slots, date=_dt.date.fromisoformat(stamp))

    def due(
        self,
        *,
        date: _dt.date | str | None = None,
        now: _dt.datetime | None = None,
        grace_minutes: int = 90,
    ) -> list[Slot]:
        """Slots whose time has arrived and that haven't been published.

        ``grace_minutes`` bounds how late a missed slot may still be posted — a
        video that was due nine hours ago should be rescheduled, not dumped out
        at 3am on top of the next one.
        """
        now = now or _dt.datetime.now(_dt.timezone.utc)
        out = []
        for slot in self.load(date):
            if slot.status != "scheduled":
                continue
            when = _dt.datetime.strptime(slot.post_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc
            )
            if when <= now <= when + _dt.timedelta(minutes=grace_minutes):
                out.append(slot)
        return out
