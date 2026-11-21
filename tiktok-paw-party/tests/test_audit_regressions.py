"""Regressions found by the release-readiness audit.

Each test here corresponds to a defect the audit surfaced. They are grouped by
what broke rather than by module, because that is how they will be read when
one of them fails again.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from pawparty.config import RenderSettings
from pawparty.media import ffmpeg


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def peak_dbfs(video: Path, start: float, duration: float) -> float:
    """Peak level of an audio window, in dBFS."""
    proc = subprocess.run(
        [ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-y",
         "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video),
         "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"max_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr)
    if not match:
        return float("-inf")
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


@pytest.fixture
def small_render() -> RenderSettings:
    return RenderSettings(width=180, height=320, fps=24, preset="ultrafast", crf=30)


# --------------------------------------------------------------------------- #
# 1. Cached clips were trusted on file size alone
# --------------------------------------------------------------------------- #
def test_is_decodable_rejects_a_missing_file(tmp_path):
    ok, why = ffmpeg.is_decodable(tmp_path / "nope.mp4")
    assert not ok and "does not exist" in why


def test_is_decodable_rejects_an_empty_file(tmp_path):
    path = tmp_path / "empty.mp4"
    path.touch()
    ok, why = ffmpeg.is_decodable(path)
    assert not ok and "empty" in why


@pytest.mark.ffmpeg
def test_is_decodable_rejects_a_truncated_download(tmp_path):
    """The failure mode this exists for: plenty of bytes, no moov atom.

    A provider download cut short by a timeout or a full disk looks complete to
    any size check, and the resume logic used to reuse it — failing three
    stages later with an error pointing at the wrong place.
    """
    path = tmp_path / "truncated.mp4"
    path.write_bytes(b"\x00\xff" * 8192)
    ok, why = ffmpeg.is_decodable(path)
    assert not ok
    assert "undecodable" in why
    assert path.stat().st_size > 1000, "the point is that size alone looks fine"


@pytest.mark.ffmpeg
def test_is_decodable_accepts_real_video(tmp_path, small_render):
    from pawparty.providers.video.synth import SynthVideoProvider

    path = tmp_path / "real.mp4"
    SynthVideoProvider().generate_clip(
        prompt="x", negative_prompt="", duration_s=1.0,
        width=small_render.width, height=small_render.height, fps=small_render.fps,
        output=path, seed=1,
    )
    ok, why = ffmpeg.is_decodable(path)
    assert ok, why


@pytest.mark.ffmpeg
def test_corrupt_cached_clip_is_discarded_and_regenerated(settings, channel):
    """End-to-end: a resumed build must survive a corrupt cached clip."""
    import shutil

    from pawparty.models import Stage
    from pawparty.pipeline import Pipeline

    settings.subtitles.min_duration_s = 0.0
    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)
    assert not result.failed, [j.error for j in result.failed]
    job = result.succeeded[0]

    work = pipeline.workspace.job_dir(pipeline.workspace.generated, job.concept.id, job.date)
    (work / "raw" / "beat_01.mp4").write_bytes(b"\x00\xff" * 4096)
    for name in ("stitched.mp4", "silent.mp4"):
        (work / name).unlink(missing_ok=True)
    shutil.rmtree(work / "prepared", ignore_errors=True)

    reloaded = pipeline.load_job(job.concept.id, job.date)
    order = Stage.order()
    for stage in order[order.index(Stage.CLIPS):]:
        reloaded.stages.pop(stage.value, None)

    rebuilt = pipeline.build(reloaded)
    assert rebuilt.status == "done"
    assert rebuilt.qc_report["passed"]


# --------------------------------------------------------------------------- #
# 2. `--seed` did not actually reproduce preview footage
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_synth_video_is_deterministic(tmp_path, small_render):
    """ffmpeg's `gradients` seeds itself randomly unless told otherwise, so the
    same --seed produced a reproducible concept and irreproducible footage."""
    from pawparty.providers.video.synth import SynthVideoProvider

    provider = SynthVideoProvider()
    outputs = []
    for i in range(3):
        path = tmp_path / f"v{i}.mp4"
        provider.generate_clip(
            prompt="a kitten mid-dance", negative_prompt="", duration_s=1.0,
            width=small_render.width, height=small_render.height,
            fps=small_render.fps, output=path, seed=11,
        )
        outputs.append(sha256(path))
    assert len(set(outputs)) == 1, "same seed produced different footage"


@pytest.mark.ffmpeg
def test_synth_video_still_varies_by_seed(tmp_path, small_render):
    from pawparty.providers.video.synth import SynthVideoProvider

    provider = SynthVideoProvider()
    hashes = []
    for seed in (11, 12):
        path = tmp_path / f"s{seed}.mp4"
        provider.generate_clip(
            prompt="a kitten mid-dance", negative_prompt="", duration_s=1.0,
            width=small_render.width, height=small_render.height,
            fps=small_render.fps, output=path, seed=seed,
        )
        hashes.append(sha256(path))
    assert hashes[0] != hashes[1], "determinism must not collapse into sameness"


@pytest.mark.ffmpeg
def test_synth_image_is_deterministic(tmp_path):
    from pawparty.providers.image.synth import SynthImageProvider

    provider = SynthImageProvider()
    hashes = []
    for i in range(2):
        path = tmp_path / f"i{i}.png"
        provider.generate_image(
            prompt="a kitten", negative_prompt="", width=180, height=320,
            output=path, seed=7,
        )
        hashes.append(sha256(path))
    assert hashes[0] == hashes[1]


@pytest.mark.ffmpeg
def test_whole_pipeline_is_byte_reproducible(settings, channel, tmp_path, monkeypatch):
    """Two full runs at the same seed must produce the identical deliverable."""
    from pawparty.pipeline import Pipeline
    from pawparty.storage import Workspace

    digests = []
    for run in range(2):
        workspace = Workspace.create(tmp_path / f"run{run}")
        pipeline = Pipeline(settings, channel, workspace=workspace)
        result = pipeline.run_batch(count=1, seed=31, concurrency=1)
        assert not result.failed, [j.error for j in result.failed]
        digests.append(sha256(Path(result.succeeded[0].final_path)))
    assert digests[0] == digests[1], "the same seed produced a different video"


# --------------------------------------------------------------------------- #
# 3. The music bed had no fade-out
# --------------------------------------------------------------------------- #
def test_render_settings_expose_both_fades():
    render = RenderSettings()
    assert render.audio_fade_in_s > 0
    assert render.audio_fade_out_s > 0


@pytest.mark.ffmpeg
def test_finished_audio_fades_out(settings, channel):
    """Without a fade-out the bed stops at full volume, which clicks audibly on
    loop restart — right at the seam the loop beat exists to hide."""
    from pawparty.pipeline import Pipeline

    settings.subtitles.min_duration_s = 0.0
    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)
    assert not result.failed, [j.error for j in result.failed]

    final = Path(result.succeeded[0].final_path)
    duration = ffmpeg.probe(final).duration_s
    fade = settings.render.audio_fade_out_s
    # Measure inside the fade, not across it: a window wider than the fade is
    # dominated by the loud samples at its start.
    window = fade / 3
    tail = peak_dbfs(final, max(0.0, duration - window), window)
    mid = peak_dbfs(final, duration / 2, 0.25)

    assert tail != float("-inf")
    assert mid - tail >= 6.0, f"tail peak {tail}dBFS vs mid {mid}dBFS — no fade-out"


@pytest.mark.ffmpeg
def test_finished_audio_fades_in(settings, channel):
    from pawparty.pipeline import Pipeline

    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)
    final = Path(result.succeeded[0].final_path)
    duration = ffmpeg.probe(final).duration_s
    window = settings.render.audio_fade_in_s / 3
    head = peak_dbfs(final, 0.0, window)
    mid = peak_dbfs(final, duration / 2, 0.25)
    assert mid - head >= 3.0, f"head peak {head}dBFS vs mid {mid}dBFS — no fade-in"


# --------------------------------------------------------------------------- #
# 4. Billing blocks were hard-coded and clamped silently
# --------------------------------------------------------------------------- #
def _replicate(**options):
    from pawparty.providers.video.replicate import ReplicateVideoProvider

    return ReplicateVideoProvider({
        "model": "owner/model", "cost_per_second_usd": 0.05,
        "input_map": {"prompt": "prompt"}, **options,
    })


def test_duration_blocks_default_to_the_common_set():
    assert _replicate().duration_blocks == [5, 6, 8, 10]


def test_duration_blocks_are_configurable():
    """Block sets differ per model; a hard-coded set is a guess."""
    provider = _replicate(duration_blocks=[4, 8, 12, 16])
    assert provider.duration_blocks == [4, 8, 12, 16]
    assert provider.billed_seconds(1.6) == 4
    assert provider.billed_seconds(9.0) == 12
    assert provider.billed_seconds(16.0) == 16
    assert provider.estimate_cost(9.0) == pytest.approx(0.60)


def test_duration_blocks_are_sorted_and_deduped():
    assert _replicate(duration_blocks=[10, 5, 5, 8, 6]).duration_blocks == [5, 6, 8, 10]


@pytest.mark.parametrize(
    ("requested", "billed"),
    [(0.7, 5), (4.99, 5), (5.0, 5), (5.01, 6), (6.0, 6), (6.01, 8), (8.0, 8), (8.01, 10)],
)
def test_block_boundaries(requested, billed):
    assert _replicate().billed_seconds(requested) == billed


def test_billing_never_understates_within_the_supported_range():
    provider = _replicate()
    for tenth in range(1, 101):
        duration = tenth / 10
        assert provider.billed_seconds(duration) >= duration


def test_over_limit_beats_are_flagged_not_silently_clamped(caplog):
    """A beat longer than the model's longest clip yields a short clip that
    assembly has to retime. That cost should be visible before a paid run."""
    import logging

    provider = _replicate()
    assert not provider.exceeds_model_limit(9.0)
    assert provider.exceeds_model_limit(12.0)

    with caplog.at_level(logging.WARNING, logger="pawparty.providers.replicate"):
        assert provider.billed_seconds(12.0) == 10
        assert provider.billed_seconds(13.0) == 10
    warnings = [r for r in caplog.records if "exceeds the longest clip" in r.message]
    assert len(warnings) == 1, "should warn once, not once per beat"


def test_no_current_beat_can_exceed_the_default_block_ceiling():
    """Defence in depth: today's role limits keep every beat under 10s, so the
    over-limit path is unreachable. If someone retunes them, this fails."""
    from pawparty.ideas.structure import ROLE_LIMITS

    longest_beat = max(ceiling for _, ceiling in ROLE_LIMITS.values())
    assert longest_beat <= max(_replicate().duration_blocks)
