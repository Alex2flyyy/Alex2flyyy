"""Spend guard.

An automated pipeline that calls paid APIs in a loop is one config typo away
from an expensive night. Two limits, both enforced *before* the call:

* **per-video** — a single video that would blow past its allowance is skipped,
  not truncated halfway.
* **per-day** — once the day's ledger crosses the cap, remaining jobs stop.

The ledger is an append-only JSONL file, so it survives restarts and doubles as
a cost report (``pawparty costs``).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from .config import BudgetSettings
from .logging_setup import get_logger
from .util import append_jsonl, iso, read_jsonl, today_stamp

log = get_logger("budget")


class BudgetExceeded(RuntimeError):
    """Raised when a planned spend would cross a configured cap."""


@dataclass
class SpendEntry:
    date: str
    concept_id: str
    stage: str
    provider: str
    amount_usd: float
    ts: str


class BudgetGuard:
    """Tracks and enforces provider spend."""

    def __init__(self, settings: BudgetSettings, ledger_path: Path) -> None:
        self.settings = settings
        self.ledger_path = Path(ledger_path)

    # -- reads --------------------------------------------------------------- #
    def spent_on(self, date: _dt.date | str | None = None) -> float:
        stamp = date if isinstance(date, str) else today_stamp(date)
        return round(
            sum(
                float(row.get("amount_usd", 0.0))
                for row in read_jsonl(self.ledger_path)
                if row.get("date") == stamp
            ),
            4,
        )

    def spent_by_concept(self, concept_id: str) -> float:
        return round(
            sum(
                float(row.get("amount_usd", 0.0))
                for row in read_jsonl(self.ledger_path)
                if row.get("concept_id") == concept_id
            ),
            4,
        )

    def remaining_today(self, date: _dt.date | str | None = None) -> float:
        return round(max(0.0, self.settings.daily_usd - self.spent_on(date)), 4)

    # -- enforcement --------------------------------------------------------- #
    def check(
        self,
        amount_usd: float,
        *,
        concept_id: str,
        date: _dt.date | str | None = None,
    ) -> None:
        """Raise :class:`BudgetExceeded` if this spend isn't allowed."""
        if amount_usd <= 0:
            return
        if not self.settings.abort_on_exceed:
            return

        per_video = self.spent_by_concept(concept_id) + amount_usd
        if per_video > self.settings.per_video_usd:
            raise BudgetExceeded(
                f"video {concept_id} would reach ${per_video:.2f}, over the "
                f"${self.settings.per_video_usd:.2f} per-video cap"
            )

        daily = self.spent_on(date) + amount_usd
        if daily > self.settings.daily_usd:
            raise BudgetExceeded(
                f"daily spend would reach ${daily:.2f}, over the "
                f"${self.settings.daily_usd:.2f} cap. Raise budget.daily_usd or "
                f"PAWPARTY_DAILY_BUDGET_USD to continue."
            )

    def record(
        self,
        amount_usd: float,
        *,
        concept_id: str,
        stage: str,
        provider: str,
        date: _dt.date | str | None = None,
    ) -> None:
        if amount_usd <= 0:
            return
        stamp = date if isinstance(date, str) else today_stamp(date)
        append_jsonl(
            self.ledger_path,
            {
                "date": stamp,
                "concept_id": concept_id,
                "stage": stage,
                "provider": provider,
                "amount_usd": round(float(amount_usd), 5),
                "ts": iso(),
            },
        )

    # -- reporting ----------------------------------------------------------- #
    def report(self, days: int = 14) -> list[tuple[str, float]]:
        totals: dict[str, float] = {}
        for row in read_jsonl(self.ledger_path):
            stamp = str(row.get("date", ""))
            totals[stamp] = totals.get(stamp, 0.0) + float(row.get("amount_usd", 0.0))
        ordered = sorted(totals.items(), reverse=True)[:days]
        return [(stamp, round(value, 4)) for stamp, value in ordered]
