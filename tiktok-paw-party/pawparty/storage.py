"""Workspace layout.

Everything the pipeline writes lives under a single workspace root so the whole
output tree can be backed up, wiped, or moved to external storage as one unit.

    Videos/
      Generated/   raw per-beat clips straight from the video provider
      Images/      first frames, thumbnails, cover stills
      Audio/       music beds (library/ holds your licensed source tracks)
      Captions/    caption options + chosen caption + hashtags, per video
      Prompts/     the exact prompt text sent to every provider (audit trail)
      Logs/        JSONL run logs, cost ledger, novelty database
      Scheduled/   per-day posting queue, one JSON per slot
      Finished/    the final 9:16 MP4s plus their post-ready sidecars

Per-video work is partitioned by date and concept id, e.g.::

    Videos/Generated/2026-08-06/disco-kittens-1a2b3c4d/beat_01.mp4

so a day's output is trivially inspectable and a failed job never collides with
a retry.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from .util import today_stamp

#: Top-level folders created inside the workspace root.
TOP_LEVEL_DIRS = (
    "Generated",
    "Images",
    "Audio",
    "Captions",
    "Prompts",
    "Logs",
    "Scheduled",
    "Finished",
)


@dataclass(frozen=True)
class Workspace:
    """Resolves every path the pipeline needs. Never build paths by hand."""

    root: Path

    # -- construction ------------------------------------------------------ #
    @classmethod
    def create(cls, root: Path | str) -> "Workspace":
        workspace = cls(Path(root).expanduser().resolve())
        workspace.ensure()
        return workspace

    def ensure(self) -> "Workspace":
        """Create the full folder tree. Idempotent."""
        self.root.mkdir(parents=True, exist_ok=True)
        for name in TOP_LEVEL_DIRS:
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "Audio" / "library").mkdir(exist_ok=True)
        (self.root / "Audio" / "beds").mkdir(exist_ok=True)
        return self

    # -- top-level ---------------------------------------------------------- #
    @property
    def generated(self) -> Path:
        return self.root / "Generated"

    @property
    def images(self) -> Path:
        return self.root / "Images"

    @property
    def audio(self) -> Path:
        return self.root / "Audio"

    @property
    def audio_library(self) -> Path:
        """Where the operator drops licensed music files + `library.yaml`."""
        return self.audio / "library"

    @property
    def audio_beds(self) -> Path:
        """Rendered/trimmed beds actually mixed into videos."""
        return self.audio / "beds"

    @property
    def captions(self) -> Path:
        return self.root / "Captions"

    @property
    def prompts(self) -> Path:
        return self.root / "Prompts"

    @property
    def logs(self) -> Path:
        return self.root / "Logs"

    @property
    def scheduled(self) -> Path:
        return self.root / "Scheduled"

    @property
    def finished(self) -> Path:
        return self.root / "Finished"

    # -- per-run / per-job -------------------------------------------------- #
    def day(self, folder: Path, date: _dt.date | str | None = None) -> Path:
        stamp = date if isinstance(date, str) else today_stamp(date)
        path = folder / stamp
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_dir(self, folder: Path, concept_id: str, date: _dt.date | str | None = None) -> Path:
        path = self.day(folder, date) / concept_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_log(self, date: _dt.date | str | None = None) -> Path:
        stamp = date if isinstance(date, str) else today_stamp(date)
        return self.logs / f"run-{stamp}.jsonl"

    @property
    def cost_ledger(self) -> Path:
        return self.logs / "costs.jsonl"

    @property
    def novelty_db(self) -> Path:
        return self.logs / "novelty.sqlite3"

    @property
    def state_db(self) -> Path:
        return self.logs / "jobs.sqlite3"

    def manifest_path(self, concept_id: str, date: _dt.date | str | None = None) -> Path:
        return self.job_dir(self.generated, concept_id, date) / "manifest.json"

    def finished_dir(self, concept_id: str, date: _dt.date | str | None = None) -> Path:
        """Post-ready bundle: `final.mp4`, `caption.txt`, `hashtags.txt`, cover."""
        return self.job_dir(self.finished, concept_id, date)

    # -- housekeeping ------------------------------------------------------- #
    def purge_intermediates(self, concept_id: str, date: _dt.date | str | None = None) -> None:
        """Drop per-beat working files once `Finished/` holds the deliverable.

        Keeps `manifest.json` so the job stays auditable and re-runnable.
        """
        job = self.job_dir(self.generated, concept_id, date)
        for child in job.iterdir():
            if child.name == "manifest.json":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def disk_free_mb(self) -> float:
        usage = shutil.disk_usage(self.root)
        return usage.free / (1024 * 1024)
