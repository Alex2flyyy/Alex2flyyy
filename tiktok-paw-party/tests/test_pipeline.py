"""Pipeline, storage, budget, scheduling and publishing."""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from pawparty.budget import BudgetExceeded, BudgetGuard
from pawparty.config import BudgetSettings
from pawparty.models import CopyPack, JobRecord, JobStatus, Stage
from pawparty.pipeline import Pipeline
from pawparty.publishing import manual_export
from pawparty.scheduling.scheduler import Scheduler
from pawparty.storage import TOP_LEVEL_DIRS, Workspace


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_workspace_creates_the_documented_tree(tmp_path):
    workspace = Workspace.create(tmp_path / "Videos")
    for name in TOP_LEVEL_DIRS:
        assert (workspace.root / name).is_dir(), f"missing {name}/"
    assert workspace.audio_library.is_dir()


def test_workspace_creation_is_idempotent(tmp_path):
    Workspace.create(tmp_path / "Videos")
    Workspace.create(tmp_path / "Videos")  # must not raise


def test_job_paths_partition_by_date_and_concept(workspace):
    path = workspace.job_dir(workspace.generated, "abc123", "2026-08-06")
    assert path.name == "abc123"
    assert path.parent.name == "2026-08-06"


def test_purge_keeps_the_manifest(workspace):
    job = workspace.job_dir(workspace.generated, "abc", "2026-08-06")
    (job / "manifest.json").write_text("{}", encoding="utf-8")
    (job / "raw").mkdir()
    (job / "raw" / "beat_01.mp4").write_bytes(b"x")

    workspace.purge_intermediates("abc", "2026-08-06")
    assert (job / "manifest.json").exists()
    assert not (job / "raw").exists()


# --------------------------------------------------------------------------- #
# Job records
# --------------------------------------------------------------------------- #
def test_job_round_trips_through_json(concept):
    job = JobRecord(concept=concept, date="2026-08-06")
    job.copy = CopyPack(
        captions=["a", "b"], chosen_caption="a", hashtags=["cutepets"],
        onscreen_hook="WAIT FOR IT",
    )
    job.begin_stage(Stage.CLIPS)
    job.finish_stage(Stage.CLIPS, detail="done", cost_usd=0.25)

    restored = JobRecord.from_dict(json.loads(json.dumps(job.to_dict())))
    assert restored.concept.id == concept.id
    assert restored.copy.chosen_caption == "a"
    assert restored.is_stage_done(Stage.CLIPS)
    assert restored.cost_usd == 0.25


def test_stage_costs_accumulate(concept):
    job = JobRecord(concept=concept, date="2026-08-06")
    job.finish_stage(Stage.CLIPS, cost_usd=0.20)
    job.finish_stage(Stage.MUSIC, cost_usd=0.05)
    assert job.cost_usd == pytest.approx(0.25)


def test_stage_order_starts_with_concept_and_ends_with_package():
    order = [s.value for s in Stage.order()]
    assert order[0] == "concept"
    assert "qc" in order and "package" in order


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def test_spend_is_recorded_and_totalled(tmp_path):
    guard = BudgetGuard(BudgetSettings(), tmp_path / "costs.jsonl")
    guard.record(0.30, concept_id="c1", stage="clips", provider="replicate", date="2026-08-06")
    guard.record(0.20, concept_id="c2", stage="clips", provider="replicate", date="2026-08-06")
    assert guard.spent_on("2026-08-06") == pytest.approx(0.50)
    assert guard.spent_by_concept("c1") == pytest.approx(0.30)


def test_daily_cap_blocks_further_spend(tmp_path):
    guard = BudgetGuard(BudgetSettings(daily_usd=1.0, per_video_usd=10.0), tmp_path / "c.jsonl")
    guard.record(0.90, concept_id="c1", stage="clips", provider="x", date="2026-08-06")
    with pytest.raises(BudgetExceeded, match="daily"):
        guard.check(0.50, concept_id="c2", date="2026-08-06")


def test_per_video_cap_blocks_a_runaway_job(tmp_path):
    guard = BudgetGuard(BudgetSettings(daily_usd=100.0, per_video_usd=0.5), tmp_path / "c.jsonl")
    guard.record(0.40, concept_id="c1", stage="clips", provider="x", date="2026-08-06")
    with pytest.raises(BudgetExceeded, match="per-video"):
        guard.check(0.30, concept_id="c1", date="2026-08-06")


def test_free_providers_never_trip_the_guard(tmp_path):
    guard = BudgetGuard(BudgetSettings(daily_usd=0.0), tmp_path / "c.jsonl")
    guard.check(0.0, concept_id="c1")  # must not raise


def test_disabling_the_guard_allows_overspend(tmp_path):
    guard = BudgetGuard(
        BudgetSettings(daily_usd=0.01, abort_on_exceed=False), tmp_path / "c.jsonl"
    )
    guard.check(99.0, concept_id="c1")  # must not raise


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def test_missing_credentials_degrade_to_synth(settings, workspace, monkeypatch):
    """A missing key should cost you quality, not the whole night's batch."""
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    settings.providers.video = "replicate"
    settings.providers.options["replicate"] = {"model": "owner/model"}

    from pawparty.providers.registry import build_providers

    bundle = build_providers(settings, workspace)
    assert bundle.video.name == "synth"
    assert bundle.degraded


def test_synth_bundle_is_flagged_as_preview_only(settings, workspace):
    from pawparty.providers.registry import build_providers

    assert build_providers(settings, workspace).is_preview_only


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
def _finished_job(concept, tmp_path, caption="Which one wins? 🐾"):
    video = tmp_path / f"{concept.id}.mp4"
    video.write_bytes(b"not a real mp4, but it exists")
    job = JobRecord(concept=concept, date="2026-08-06", status=JobStatus.DONE.value)
    job.final_path = str(video)
    job.copy = CopyPack(
        captions=[caption], chosen_caption=caption, hashtags=["cutepets", "fyp"],
        onscreen_hook="WAIT FOR IT", comment_seed="Who won?",
    )
    return job


def test_schedule_assigns_one_slot_per_video(settings, workspace, generator, tmp_path):
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(7, seed=1)]
    slots = Scheduler(settings.schedule, workspace).plan(jobs, date=_dt.date(2026, 8, 6))

    assert len(slots) == 7
    assert len({s.concept_id for s in slots}) == 7
    times = [s.post_at_local for s in slots]
    assert times == sorted(times), "slots must be chronological"


def test_slots_are_spread_across_the_day(settings, workspace, generator, tmp_path):
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(7, seed=2)]
    slots = Scheduler(settings.schedule, workspace).plan(jobs, date=_dt.date(2026, 8, 6))
    hours = [int(s.post_at_local[11:13]) for s in slots]
    assert max(hours) - min(hours) >= 8, "posts are bunched together"


def test_more_videos_than_slots_still_schedules_all(settings, workspace, generator, tmp_path):
    settings.schedule.post_times = ["09:00", "18:00"]
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(5, seed=3)]
    slots = Scheduler(settings.schedule, workspace).plan(jobs, date=_dt.date(2026, 8, 6))
    assert len(slots) == 5
    assert len({s.post_at_local for s in slots}) == 5, "slots collided"


def test_videos_without_a_file_are_not_scheduled(settings, workspace, concept):
    job = JobRecord(concept=concept, date="2026-08-06", status=JobStatus.DONE.value)
    job.final_path = "/does/not/exist.mp4"
    assert Scheduler(settings.schedule, workspace).plan([job]) == []


def test_queue_survives_a_round_trip(settings, workspace, generator, tmp_path):
    scheduler = Scheduler(settings.schedule, workspace)
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(3, seed=4)]
    planned = scheduler.plan(jobs, date=_dt.date(2026, 8, 6))
    loaded = scheduler.load("2026-08-06")
    assert [s.concept_id for s in loaded] == [s.concept_id for s in planned]


def test_due_ignores_slots_that_have_not_arrived(settings, workspace, generator, tmp_path):
    scheduler = Scheduler(settings.schedule, workspace)
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(3, seed=5)]
    scheduler.plan(jobs, date=_dt.date(2026, 8, 6))
    early = _dt.datetime(2026, 8, 6, 0, 0, tzinfo=_dt.timezone.utc)
    assert scheduler.due(date="2026-08-06", now=early) == []


def test_due_ignores_slots_that_are_far_too_late(settings, workspace, generator, tmp_path):
    """A slot missed by nine hours should be rescheduled, not dumped at 3am."""
    scheduler = Scheduler(settings.schedule, workspace)
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(3, seed=6)]
    scheduler.plan(jobs, date=_dt.date(2026, 8, 6))
    late = _dt.datetime(2026, 8, 8, tzinfo=_dt.timezone.utc)
    assert scheduler.due(date="2026-08-06", now=late) == []


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def test_manual_export_produces_a_post_ready_folder(
    settings, workspace, generator, tmp_path
):
    jobs = [_finished_job(c, tmp_path) for c in generator.generate_batch(2, seed=7)]
    slots = Scheduler(settings.schedule, workspace).plan(jobs, date=_dt.date(2026, 8, 6))
    root = manual_export(slots, workspace, date=_dt.date(2026, 8, 6))

    assert (root / "README.md").exists()
    folders = [p for p in root.iterdir() if p.is_dir()]
    assert len(folders) == 2
    for folder in folders:
        assert (folder / "final.mp4").exists()
        assert (folder / "POST.txt").exists()
        details = (folder / "details.md").read_text(encoding="utf-8")
        assert "AI-generated" in details, "the AI disclosure reminder is mandatory"


def test_tiktok_publisher_reports_when_it_cannot_run(monkeypatch):
    from pawparty.providers.base import ProviderUnavailable
    from pawparty.publishing.tiktok_api import TikTokPublisher

    monkeypatch.delenv("TIKTOK_ACCESS_TOKEN", raising=False)
    publisher = TikTokPublisher()
    assert not publisher.available()
    with pytest.raises(ProviderUnavailable, match="docs/PUBLISHING.md"):
        publisher.require()


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_full_pipeline_produces_a_publishable_file(settings, channel):
    """The whole thing, for real, with ffmpeg — no mocks."""
    settings.subtitles.min_duration_s = 0.0  # exercise the burn-in path
    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)

    assert not result.failed, [j.error for j in result.failed]
    job = result.succeeded[0]

    from pathlib import Path

    final = Path(job.final_path)
    assert final.exists()
    assert job.qc_report["passed"]
    assert job.qc_report["info"]["width"] == 1080
    assert job.qc_report["info"]["height"] == 1920
    assert job.qc_report["info"]["has_audio"]

    bundle = final.parent
    for name in ("caption.txt", "hashtags.txt", "post.txt", "concept.json"):
        assert (bundle / name).exists(), f"missing {name} in the finished bundle"


@pytest.mark.ffmpeg
def test_a_completed_job_resumes_without_redoing_work(settings, channel):
    """Re-running a finished job must be a no-op, not a re-render."""
    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=32, concurrency=1)
    job = result.succeeded[0]

    from pathlib import Path

    mtime = Path(job.final_path).stat().st_mtime
    reloaded = pipeline.load_job(job.concept.id, job.date)
    pipeline.build(reloaded)
    assert Path(job.final_path).stat().st_mtime == mtime
