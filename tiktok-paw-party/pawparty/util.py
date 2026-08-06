"""Small shared helpers: ids, slugs, seeded randomness, JSON/YAML io, retries.

Kept dependency-light on purpose — everything here is safe to import from any
other module without creating cycles.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar

T = TypeVar("T")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utc_now() -> _dt.datetime:
    """Timezone-aware current UTC time (never use naive datetimes internally)."""
    return _dt.datetime.now(_dt.timezone.utc)


def iso(ts: _dt.datetime | None = None) -> str:
    """ISO-8601 string with a `Z` suffix, e.g. `2026-08-06T14:03:11Z`."""
    ts = ts or utc_now()
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_stamp(date: _dt.date | None = None) -> str:
    """`YYYY-MM-DD` — used for the daily folder partitions."""
    return (date or utc_now().date()).isoformat()


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
def slugify(text: str, max_len: int = 48) -> str:
    """Filesystem-safe lowercase slug. Collapses runs of punctuation to `-`."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def short_hash(*parts: Any, length: int = 8) -> str:
    """Stable short digest of the given parts — used for concept ids."""
    payload = "|".join(str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


#: Irregular plurals we actually use. Anything else falls through to the rules.
_PLURALS = {"mouse": "mice", "goose": "geese", "person": "people", "wolf": "wolves"}


def pluralize(word: str) -> str:
    """Enough English to keep titles and hashtags readable.

    A full inflection library is overkill for a vocabulary of animal nouns; this
    handles the `-y -> -ies` case that otherwise produces "puppys".
    """
    lowered = word.lower()
    if lowered in _PLURALS:
        return _PLURALS[lowered]
    if lowered.endswith("y") and len(lowered) > 1 and lowered[-2] not in "aeiou":
        return word[:-1] + "ies"
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


#: Leading articles stripped when a label is spliced into a title.
_ARTICLES = ("a ", "an ", "the ")


def strip_article(text: str) -> str:
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            return text[len(article):]
    return text


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Truncate on a word boundary when possible."""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ellipsis)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + ellipsis


# --------------------------------------------------------------------------- #
# Randomness
# --------------------------------------------------------------------------- #
def seeded_rng(*parts: Any) -> random.Random:
    """A :class:`random.Random` derived deterministically from ``parts``.

    Same inputs always produce the same stream, which is what makes a concept
    reproducible from its seed alone (see ``pawparty ideas --seed``).
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def weighted_choice(rng: random.Random, items: Sequence[T], weights: Sequence[float]) -> T:
    """Pick one item by weight. Raises ValueError when nothing is selectable."""
    if not items:
        raise ValueError("weighted_choice() received an empty pool")
    total = float(sum(weights))
    if total <= 0:
        return rng.choice(list(items))
    target = rng.random() * total
    cumulative = 0.0
    for item, weight in zip(items, weights):
        cumulative += float(weight)
        if target <= cumulative:
            return item
    return items[-1]


def sample_without_replacement(
    rng: random.Random, items: Sequence[T], weights: Sequence[float], k: int
) -> list[T]:
    """Weighted sample of ``k`` distinct items (clamped to pool size)."""
    pool = list(items)
    pool_weights = list(weights)
    picked: list[T] = []
    for _ in range(min(k, len(pool))):
        choice = weighted_choice(rng, pool, pool_weights)
        index = pool.index(choice)
        pool.pop(index)
        pool_weights.pop(index)
        picked.append(choice)
    return picked


def jitter(rng: random.Random, value: float, spread: float) -> float:
    """``value`` nudged by up to ±``spread`` (uniform)."""
    return value + rng.uniform(-spread, spread)


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any, *, indent: int = 2) -> Path:
    """Atomic-ish JSON write (temp file + replace) so readers never see halves."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_text(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Reliability
# --------------------------------------------------------------------------- #
def retry(
    func: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_error: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call ``func`` with exponential backoff (2s, 4s, 8s, …).

    Re-raises the final exception once ``attempts`` is exhausted.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except exceptions as exc:  # noqa: PERF203 - retry loop is the point
            last = exc
            if on_error:
                on_error(attempt, exc)
            if attempt == attempts:
                break
            time.sleep(base_delay * (2 ** (attempt - 1)))
    assert last is not None
    raise last


def chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
