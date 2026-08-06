"""CSV export."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from leadgen.logging import get_logger

log = get_logger(__name__)


def write_csv(
    rows: list[dict[str, Any]], path: Path | str, columns: list[str] | None = None
) -> Path:
    from leadgen.reports.daily_report import EXPORT_COLUMNS

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or EXPORT_COLUMNS

    # utf-8-sig writes a BOM. Without it Excel on Windows renders accented
    # business names as mojibake, and this file is opened in Excel constantly.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _clean(row.get(c, "")) for c in columns})

    log.info("export.csv", path=str(path), rows=len(rows))
    return path


def _clean(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    text = str(value)
    # A leading =, +, -, or @ makes Excel treat the cell as a formula. With
    # attacker-influenced content (business names come from the open web) that
    # is CSV injection; prefixing an apostrophe neutralizes it.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text
