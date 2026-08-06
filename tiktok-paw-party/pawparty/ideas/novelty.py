"""Novelty ledger — the anti-repetition memory.

Two mechanisms, both backed by one SQLite file in `Logs/novelty.sqlite3`:

1. **Signature rejection.** Every concept reduces to a signature (the sorted set
   of its key option ids). If an identical signature was used in the last
   ``lookback`` videos, the concept is rejected and regenerated with a new seed.

2. **Soft recency penalty.** Every option id used recently gets a weight
   multiplier < 1 that decays back to 1.0 over ``decay_window`` videos. This is
   fed straight into :class:`~pawparty.ideas.vocabulary.VocabularySampler`, so
   the sampler naturally drifts toward under-used costumes, settings and dance
   styles instead of hard-banning anything.

Similarity between two concepts is Jaccard overlap of their option sets, which
catches "same thing with one hat swapped" — the failure mode that actually kills
a faceless account.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ..util import iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    signature   TEXT NOT NULL,
    options     TEXT NOT NULL,
    title       TEXT,
    created_at  TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_concepts_channel ON concepts(channel, seq);
CREATE TABLE IF NOT EXISTS option_usage (
    channel     TEXT NOT NULL,
    option_id   TEXT NOT NULL,
    concept_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_channel ON option_usage(channel, option_id);
"""


@dataclass
class NoveltyConfig:
    #: How many recent concepts participate in duplicate detection.
    lookback: int = 400
    #: Reject when Jaccard similarity against any recent concept exceeds this.
    max_similarity: float = 0.72
    #: Options used within this many recent videos get down-weighted.
    decay_window: int = 60
    #: Strength of the penalty for the most recently used option (0-1).
    penalty_strength: float = 0.82


class NoveltyLedger:
    """Persistent memory of what has already been made."""

    def __init__(self, db_path: Path, config: NoveltyConfig | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or NoveltyConfig()
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # -- reads --------------------------------------------------------------- #
    def recent(self, channel: str, limit: int | None = None) -> list[tuple[str, set[str]]]:
        """Most recent ``(concept_id, option_set)`` pairs, newest first."""
        limit = limit or self.config.lookback
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, options FROM concepts WHERE channel = ? "
                "ORDER BY seq DESC LIMIT ?",
                (channel, limit),
            ).fetchall()
        return [(row[0], set(filter(None, row[1].split(",")))) for row in rows]

    def _next_seq(self, conn: sqlite3.Connection, channel: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM concepts WHERE channel = ?", (channel,)
        ).fetchone()
        return int(row[0]) + 1

    def count(self, channel: str | None = None) -> int:
        with closing(self._connect()) as conn:
            if channel:
                row = conn.execute(
                    "SELECT COUNT(*) FROM concepts WHERE channel = ?", (channel,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM concepts").fetchone()
        return int(row[0])

    def recency_penalty(self, channel: str) -> dict[str, float]:
        """Weight multipliers keyed by ``"<pool>:<option_id>"``.

        An option used in the most recent video gets ``1 - penalty_strength``;
        one used ``decay_window`` videos ago is back to 1.0, with linear decay
        in between — predictable and easy to reason about when tuning.

        The distance is measured in *videos*, using the per-channel ``seq``
        counter. An earlier version inferred it from SQLite rowids divided by an
        estimated options-per-video figure; that estimate drifted badly as the
        library grew, which quietly shortened the decay window over time.
        """
        window = self.config.decay_window
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT option_id, MAX(seq) AS last_seen
                FROM option_usage WHERE channel = ?
                GROUP BY option_id
                """,
                (channel,),
            ).fetchall()
            latest = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM concepts WHERE channel = ?", (channel,)
            ).fetchone()[0]

        if not rows or not latest:
            return {}

        penalties: dict[str, float] = {}
        for option_id, last_seen in rows:
            videos_ago = latest - int(last_seen)
            if videos_ago >= window:
                continue
            freshness = videos_ago / window  # 0 = just used, 1 = fully decayed
            penalties[option_id] = 1.0 - self.config.penalty_strength * (1.0 - freshness)
        return penalties

    # -- checks -------------------------------------------------------------- #
    @staticmethod
    def similarity(a: Iterable[str], b: Iterable[str]) -> float:
        set_a, set_b = set(a), set(b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    def is_novel(self, channel: str, options: Sequence[str]) -> tuple[bool, str]:
        """``(ok, reason)`` — reason explains the rejection when ``ok`` is False."""
        option_set = set(options)
        for concept_id, previous in self.recent(channel):
            if previous == option_set:
                return False, f"exact duplicate of {concept_id}"
            score = self.similarity(option_set, previous)
            if score > self.config.max_similarity:
                return False, f"{score:.0%} similar to {concept_id}"
        return True, ""

    # -- writes -------------------------------------------------------------- #
    def record(
        self,
        channel: str,
        concept_id: str,
        signature: str,
        options: Sequence[str],
        title: str = "",
    ) -> None:
        now = iso()
        with closing(self._connect()) as conn:
            # Re-recording an existing concept must not advance the counter, or
            # a resumed build would age every option in the library by one.
            existing = conn.execute(
                "SELECT seq FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            seq = int(existing[0]) if existing else self._next_seq(conn, channel)

            conn.execute(
                "INSERT OR REPLACE INTO concepts "
                "(id, channel, signature, options, title, created_at, seq) "
                "VALUES (?,?,?,?,?,?,?)",
                (concept_id, channel, signature, ",".join(sorted(options)), title, now, seq),
            )
            conn.execute("DELETE FROM option_usage WHERE concept_id = ?", (concept_id,))
            conn.executemany(
                "INSERT INTO option_usage (channel, option_id, concept_id, seq, created_at) "
                "VALUES (?,?,?,?,?)",
                [
                    (channel, option_id, concept_id, seq, now)
                    for option_id in sorted(set(options))
                ],
            )
            conn.commit()

    def forget(self, concept_id: str) -> None:
        """Remove a concept — use when a job fails before anything is published."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
            conn.execute("DELETE FROM option_usage WHERE concept_id = ?", (concept_id,))
            conn.commit()

    def option_leaderboard(self, channel: str, limit: int = 20) -> list[tuple[str, int]]:
        """Most-used options — handy for spotting an over-represented costume."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT option_id, COUNT(*) c FROM option_usage WHERE channel = ? "
                "GROUP BY option_id ORDER BY c DESC LIMIT ?",
                (channel, limit),
            ).fetchall()
        return [(row[0], int(row[1])) for row in rows]
