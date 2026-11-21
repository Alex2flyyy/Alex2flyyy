"""Loop-beat reuse: derivation, fallbacks, determinism and cost accounting.

The behaviour under test is a cost optimisation that touches the deliverable,
so these tests care about two things equally: that the money is counted right,
and that the video is not made worse.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pawparty.config import RenderSettings
from pawparty.costing import estimate_batch
from pawparty.ideas.structure import ROLE_LIMITS, assign_roles, mark_derivable_loop
from pawparty.media import ffmpeg
from pawparty.media.loop import DURATION_EPSILON_S, can_derive, derive_loop_clip
from pawparty.models import Beat


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def beat(index: int, role: str, duration: float) -> Beat:
    return Beat(
        index=index, role=role, duration_s=duration, action="dance",
        camera="static", camera_prompt="", transition_in="cut",
    )


@pytest.fixture
def small_render() -> RenderSettings:
    """A cheap render profile so ffmpeg-backed tests stay quick."""
    return RenderSettings(width=180, height=320, fps=24, preset="ultrafast", crf=30)


@pytest.fixture
def source_clip(tmp_path, small_render) -> Path:
    """A real 4-second clip standing in for beat one's generated footage."""
    from pawparty.providers.video.synth import SynthVideoProvider

    path = tmp_path / "beat_01.mp4"
    SynthVideoProvider().generate_clip(
        prompt="a kitten mid-dance", negative_prompt="", duration_s=4.0,
        width=small_render.width, height=small_render.height, fps=small_render.fps,
        output=path, seed=11,
    )
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frame_ssim(a: Path, b: Path) -> float:
    """Structural similarity between two stills, via ffmpeg's `ssim` filter.

    1.0 is identical. Re-encoding at the harsh profile these tests use costs
    roughly 0.02, so anything above ~0.95 is "the same frame".
    """
    import re
    import subprocess

    proc = subprocess.run(
        [
            ffmpeg.ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-y",
            "-i", str(a), "-i", str(b), "-lavfi", "ssim", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"All:([0-9.]+)", proc.stderr)
    if not match:
        raise AssertionError(f"ffmpeg ssim produced no score:\n{proc.stderr[-800:]}")
    return float(match.group(1))


# --------------------------------------------------------------------------- #
# Marking: which beats become derivable
# --------------------------------------------------------------------------- #
def test_loop_beat_is_marked_derivable():
    beats = [beat(1, "hook", 3.5), beat(2, "build", 5.0), beat(3, "payoff", 4.0),
             beat(4, "loop", 1.2)]
    mark_derivable_loop(beats)
    assert beats[-1].derived_from == "beat_01"
    assert beats[-1].is_derived
    assert not any(b.is_derived for b in beats[:-1])


def test_marking_is_skipped_when_disabled():
    beats = [beat(1, "hook", 3.5), beat(2, "loop", 1.2)]
    mark_derivable_loop(beats, enabled=False)
    assert not beats[-1].is_derived


def test_payoff_loop_is_never_derived():
    """A two- or three-beat video ends on the punchline, which needs its own
    footage — reusing the opening there would throw away the joke."""
    for roles in (["hook", "payoff_loop"], ["hook", "build", "payoff_loop"]):
        beats = [beat(i, r, 3.0) for i, r in enumerate(roles, start=1)]
        mark_derivable_loop(beats)
        assert not any(b.is_derived for b in beats), roles


def test_marking_is_skipped_when_the_loop_is_longer_than_beat_one():
    beats = [beat(1, "hook", 1.0), beat(2, "loop", 2.0)]
    mark_derivable_loop(beats)
    assert not beats[-1].is_derived, "there is nothing to take from beat one"


def test_single_beat_concept_is_left_alone():
    beats = [beat(1, "hook", 3.0)]
    mark_derivable_loop(beats)
    assert not beats[0].is_derived


def test_role_limits_make_loops_derivable_in_practice():
    """The loop ceiling sits below the hook floor, so the duration check should
    essentially always pass — if someone retunes the limits, this fails."""
    assert ROLE_LIMITS["loop"][1] <= ROLE_LIMITS["hook"][0]


@pytest.mark.parametrize("count", [4, 5, 6, 7, 8])
def test_longer_videos_all_end_on_a_derivable_loop(count):
    roles = assign_roles(count)
    beats = [beat(i, r, 1.2 if r == "loop" else 4.0) for i, r in enumerate(roles, start=1)]
    mark_derivable_loop(beats)
    assert beats[-1].is_derived


# --------------------------------------------------------------------------- #
# can_derive: the guard
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_short_beat_can_be_derived(source_clip):
    """The ordinary case: a 1.2s loop out of a 4s opening clip."""
    check = can_derive(source_clip, 1.2)
    assert check
    assert check.reason == ""
    assert check.source_duration_s == pytest.approx(4.0, abs=0.1)


def test_missing_first_clip_is_refused(tmp_path):
    check = can_derive(tmp_path / "nope.mp4", 1.2)
    assert not check
    assert "does not exist" in check.reason


def test_absent_source_path_is_refused():
    """The pipeline passes None when beat one produced no recorded clip."""
    check = can_derive(None, 1.2)
    assert not check
    assert "no source clip" in check.reason


def test_empty_first_clip_is_refused(tmp_path):
    path = tmp_path / "empty.mp4"
    path.touch()
    check = can_derive(path, 1.2)
    assert not check
    assert "empty" in check.reason


def test_undecodable_first_clip_is_refused(tmp_path):
    """A file that exists and has bytes but isn't video must not crash a batch."""
    path = tmp_path / "garbage.mp4"
    path.write_bytes(b"this is definitely not an mp4" * 50)
    check = can_derive(path, 1.2)
    assert not check
    assert "undecodable" in check.reason


@pytest.mark.ffmpeg
def test_incompatible_duration_is_refused(source_clip):
    """A loop longer than the source has nothing to take."""
    check = can_derive(source_clip, 9.0)
    assert not check
    assert "needs" in check.reason
    assert "9.00" in check.reason


@pytest.mark.ffmpeg
def test_duration_equal_to_the_source_is_allowed(source_clip):
    info = ffmpeg.probe(source_clip)
    assert can_derive(source_clip, info.duration_s)


@pytest.mark.ffmpeg
def test_rounding_slack_is_tolerated(source_clip):
    """A loop a hair over the source length is rounding, not a real overrun."""
    info = ffmpeg.probe(source_clip)
    assert can_derive(source_clip, info.duration_s + DURATION_EPSILON_S / 2)
    assert not can_derive(source_clip, info.duration_s + 0.5)


def test_non_positive_duration_is_refused(tmp_path):
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    assert not can_derive(path, 0.0)
    assert not can_derive(path, -1.0)


# --------------------------------------------------------------------------- #
# derive_loop_clip: the output
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_derived_clip_has_the_requested_duration(tmp_path, source_clip, small_render):
    out = derive_loop_clip(source_clip, 1.2, tmp_path / "loop.mp4", small_render)
    info = ffmpeg.probe(out)
    assert abs(info.duration_s - 1.2) < 0.1
    assert info.width == small_render.width
    assert info.height == small_render.height


@pytest.mark.ffmpeg
def test_derived_clip_starts_on_the_sources_first_frame(tmp_path, source_clip, small_render):
    """The whole point: the loop's first frame is the video's first frame.

    Measured as SSIM, not byte equality — the derived clip is re-encoded, so
    its decoded pixels cannot be bit-identical through a lossy codec. The
    companion test below confirms this measurement actually discriminates.
    """
    out = derive_loop_clip(source_clip, 1.0, tmp_path / "loop.mp4", small_render)
    a = ffmpeg.extract_frame(source_clip, tmp_path / "src.png", 0.0)
    b = ffmpeg.extract_frame(out, tmp_path / "loop.png", 0.0)

    score = frame_ssim(a, b)
    assert score > 0.95, f"derived loop opens on a different frame (SSIM {score:.3f})"


@pytest.mark.ffmpeg
def test_the_frame_comparison_can_tell_frames_apart(tmp_path, source_clip):
    """Guards the test above: a high SSIM has to mean something."""
    first = ffmpeg.extract_frame(source_clip, tmp_path / "f0.png", 0.0)
    later = ffmpeg.extract_frame(source_clip, tmp_path / "f1.png", 3.0)
    assert frame_ssim(first, later) < 0.9, "SSIM cannot distinguish these frames"


@pytest.mark.ffmpeg
def test_derivation_is_deterministic(tmp_path, source_clip, small_render):
    """Same source and duration -> identical bytes, so a resumed build reuses
    the file instead of producing a subtly different one."""
    first = derive_loop_clip(source_clip, 1.2, tmp_path / "a.mp4", small_render)
    second = derive_loop_clip(source_clip, 1.2, tmp_path / "b.mp4", small_render)
    assert sha256(first) == sha256(second)


@pytest.mark.ffmpeg
def test_derived_clip_carries_no_audio(tmp_path, source_clip, small_render):
    """Audio arrives later from the music bed; a stray track here would be
    mixed in twice and break the fade."""
    out = derive_loop_clip(source_clip, 1.0, tmp_path / "loop.mp4", small_render)
    assert not ffmpeg.probe(out).has_audio


# --------------------------------------------------------------------------- #
# Cost accounting
# --------------------------------------------------------------------------- #
class _Bundle:
    """Minimal stand-in for ProviderBundle."""

    def __init__(self, video, image=None, music=None, llm=None):
        self.video = video
        self.image = image or _FreeImage()
        self.music = music or _FreeMusic()
        self.llm = llm or _OfflineLLM()
        self.degraded: list[str] = []

    def describe(self):
        return {"video": getattr(self.video, "name", "?")}


class _FreeImage:
    name = "synth"

    def estimate_cost(self):
        return 0.0


class _FreeMusic:
    name = "synth"

    def estimate_cost(self, duration_s):
        return 0.0


class _OfflineLLM:
    name = "offline"

    def estimate_cost(self):
        return 0.0


def _replicate(cost_per_second=0.05):
    from pawparty.providers.video.replicate import ReplicateVideoProvider

    return ReplicateVideoProvider({
        "model": "owner/model",
        "cost_per_second_usd": cost_per_second,
        "input_map": {"prompt": "prompt"},
    })


def test_replicate_bills_in_discrete_blocks():
    """A 1.6s beat costs a full 5s block — which is why deriving it matters."""
    provider = _replicate()
    assert provider.billed_seconds(1.6) == 5
    assert provider.billed_seconds(4.0) == 5
    assert provider.billed_seconds(5.5) == 6
    assert provider.billed_seconds(7.0) == 8
    assert provider.billed_seconds(9.5) == 10
    assert provider.estimate_cost(1.6) == pytest.approx(0.25)


def test_default_provider_bills_per_requested_second():
    from pawparty.providers.base import BaseProvider

    assert BaseProvider().billed_seconds(3.4) == pytest.approx(3.4)


def test_estimate_excludes_derived_beats(generator, settings):
    concepts = generator.generate_batch(5, seed=909)
    bundle = _Bundle(_replicate())

    with_reuse = estimate_batch(concepts, bundle, settings)

    # Strip the flags and re-cost: that is the no-reuse baseline.
    for concept in concepts:
        for b in concept.beats:
            b.derived_from = ""
    without_reuse = estimate_batch(concepts, bundle, settings)

    assert with_reuse.derived_beats > 0, "no loop beats were derivable in this batch"
    assert with_reuse.video_requests < without_reuse.video_requests
    assert with_reuse.video_billed_seconds < without_reuse.video_billed_seconds
    assert with_reuse.video_usd < without_reuse.video_usd
    # The avoided figures must account for exactly the difference.
    assert with_reuse.video_requests + with_reuse.avoided_requests == \
        without_reuse.video_requests
    assert with_reuse.video_usd + with_reuse.avoided_usd == \
        pytest.approx(without_reuse.video_usd, abs=0.01)


def test_estimate_counts_one_still_per_generated_beat(generator, settings):
    """A derived beat needs no first frame, so it must not be charged one."""
    concepts = generator.generate_batch(3, seed=404)
    bundle = _Bundle(_replicate())
    bundle.video.input_map = {"prompt": "prompt", "first_frame": "image"}

    estimate = estimate_batch(concepts, bundle, settings)
    assert estimate.image_requests == estimate.video_requests
    total_beats = sum(len(c.beats) for c in concepts)
    assert estimate.image_requests == total_beats - estimate.derived_beats


def test_estimate_is_deterministic(generator, settings):
    bundle = _Bundle(_replicate())
    a = estimate_batch(generator.generate_batch(4, seed=7), bundle, settings)
    b = estimate_batch(generator.generate_batch(4, seed=7), bundle, settings)
    assert a.to_dict() == b.to_dict()


def test_estimate_reports_a_saving_percentage(generator, settings):
    concepts = generator.generate_batch(6, seed=121)
    estimate = estimate_batch(concepts, _Bundle(_replicate()), settings)
    assert 0 < estimate.saving_pct < 100
    assert estimate.total_usd > 0
    assert estimate.per_video_usd > 0


def test_free_providers_report_zero(generator, settings, workspace):
    from pawparty.providers.registry import build_providers

    bundle = build_providers(settings, workspace)
    estimate = estimate_batch(generator.generate_batch(3, seed=5), bundle, settings)
    assert estimate.is_free
    assert estimate.total_usd == 0.0
    assert estimate.video_requests > 0


def test_concepts_are_flagged_when_the_setting_is_on(generator, settings):
    assert settings.reuse_loop_clip
    concepts = generator.generate_batch(8, seed=31)
    assert any(b.is_derived for c in concepts for b in c.beats)


def test_no_concepts_are_flagged_when_the_setting_is_off(settings, channel):
    from pawparty.ideas.concept import ConceptGenerator

    settings.reuse_loop_clip = False
    generator = ConceptGenerator(settings, channel)
    concepts = [generator.generate_one(seed=s) for s in range(10)]
    assert not any(b.is_derived for c in concepts for b in c.beats)


def test_derived_flag_survives_json(generator):
    from pawparty.models import Concept

    concept = next(
        c for c in generator.generate_batch(8, seed=61)
        if any(b.is_derived for b in c.beats)
    )
    restored = Concept.from_dict(concept.to_dict())
    assert [b.derived_from for b in restored.beats] == [b.derived_from for b in concept.beats]


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_pipeline_derives_the_loop_beat_and_still_passes_qc(settings, channel):
    """The saving must not cost us the video."""
    from pawparty.pipeline import Pipeline

    pipeline = Pipeline(settings, channel)
    # Seed chosen for a 4+ beat concept, so there is a `loop` beat to derive.
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)
    assert not result.failed, [j.error for j in result.failed]

    job = result.succeeded[0]
    loop_beats = [b for b in job.concept.beats if b.is_derived]
    if not loop_beats:
        pytest.skip("this concept had no derivable loop beat")

    assert job.qc_report["passed"]
    assert "derived locally" in job.stages["clips"].detail
    assert result.estimate is not None
    assert result.estimate.derived_beats == len(loop_beats)

    # The derived beat still produced a real clip of the right length.
    raw = Path(job.clip_paths[-1])
    assert raw.exists()
    assert abs(ffmpeg.probe(raw).duration_s - job.concept.beats[-1].duration_s) < 0.25


@pytest.mark.ffmpeg
def test_estimate_only_generates_nothing(settings, channel):
    from pawparty.pipeline import Pipeline

    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=2, seed=77, estimate_only=True)

    assert result.estimate is not None
    assert result.estimate.videos == 2
    assert not result.succeeded and not result.failed
    assert not list(pipeline.workspace.finished.rglob("final.mp4"))


@pytest.mark.ffmpeg
def test_pipeline_falls_back_when_the_source_clip_is_unusable(settings, channel, monkeypatch):
    """Derivation is an optimisation, not a dependency: if beat one's clip is
    unreadable, the loop beat must be generated rather than the job failing."""
    from pawparty.media import loop as media_loop
    from pawparty.pipeline import Pipeline

    monkeypatch.setattr(
        media_loop, "can_derive",
        lambda source, duration: media_loop.DerivationCheck(False, "forced failure"),
    )

    pipeline = Pipeline(settings, channel)
    result = pipeline.run_batch(count=1, seed=31, concurrency=1)

    assert not result.failed, [j.error for j in result.failed]
    job = result.succeeded[0]
    assert job.qc_report["passed"]
    assert "0 derived locally" in job.stages["clips"].detail
    # The flag is cleared so the manifest reflects what actually happened.
    assert not any(b.is_derived for b in job.concept.beats)
