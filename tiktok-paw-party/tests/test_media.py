"""Media layer: filter construction, subtitles, QC, and real ffmpeg renders."""

from __future__ import annotations

import pytest

from pawparty.config import RenderSettings, SubtitleSettings
from pawparty.media import ffmpeg, subtitles
from pawparty.media.assemble import TRANSITIONS
from pawparty.media.motion import MOTIONS, build_beat_chain, motion_filter, normalize_filter
from pawparty.media.qc import QCReport, check_video
from pawparty.models import Beat


@pytest.fixture
def render() -> RenderSettings:
    return RenderSettings()


# --------------------------------------------------------------------------- #
# Filter construction (no ffmpeg needed)
# --------------------------------------------------------------------------- #
def test_normalize_covers_and_crops(render):
    chain = normalize_filter(render)
    assert "force_original_aspect_ratio=increase" in chain
    assert f"crop={render.width}:{render.height}" in chain
    assert "setsar=1" in chain


@pytest.mark.parametrize("move_id", sorted(MOTIONS))
def test_every_motion_builds_a_chain(move_id, render):
    chain = motion_filter(move_id, render, 5.0)
    assert chain
    assert "  " not in chain


def test_unknown_motion_degrades_to_a_default(render):
    assert motion_filter("does_not_exist", render, 4.0) == motion_filter(
        "handheld_float", render, 4.0
    )


def test_beat_chain_ends_in_the_right_format(render):
    chain = build_beat_chain(render, "slow_push_in", 4.0)
    assert chain.endswith(f"format={render.pixel_format}")
    assert f"fps={render.fps}" in chain


def test_transitions_map_to_real_xfade_names():
    """A typo here renders as a hard failure mid-batch."""
    valid = {
        "fade", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
        "slideright", "slideup", "slidedown", "circlecrop", "rectcrop", "distance",
        "fadeblack", "fadewhite", "radial", "smoothleft", "smoothright", "smoothup",
        "smoothdown", "circleopen", "circleclose", "vertopen", "vertclose",
        "horzopen", "horzclose", "dissolve", "pixelize", "diagtl", "diagtr",
        "diagbl", "diagbr", "hlslice", "hrslice", "vuslice", "vdslice",
    }
    for name, (xfade_name, duration) in TRANSITIONS.items():
        if not xfade_name:
            continue
        assert xfade_name in valid, f"{name} maps to unknown xfade {xfade_name!r}"
        assert 0 < duration < 1.0


def test_filter_text_escaping():
    escaped = ffmpeg.escape_filter_text("time: 10% [x], y")
    for char in (":", "%", ",", "[", "]"):
        assert f"\\{char}" in escaped


# --------------------------------------------------------------------------- #
# Subtitles
# --------------------------------------------------------------------------- #
def test_ass_file_has_styles_and_events(tmp_path, render):
    cfg = SubtitleSettings()
    lines = [
        {"start": 0.0, "end": 2.0, "text": "WAIT FOR IT", "emphasis": "hook"},
        {"start": 5.0, "end": 7.0, "text": "WHO WON?", "emphasis": "cta"},
    ]
    path = subtitles.build_ass(lines, render, cfg, tmp_path / "s.ass")
    content = path.read_text(encoding="utf-8")

    assert "[V4+ Styles]" in content
    assert "Style: Hook," in content
    assert content.count("Dialogue:") == 2
    assert "WAIT FOR IT" in content
    assert f"PlayResX: {render.width}" in content


def test_subtitles_stay_inside_the_safe_area(tmp_path, render):
    cfg = SubtitleSettings()
    path = subtitles.build_ass(
        [{"start": 0.0, "end": 1.0, "text": "HI", "emphasis": "hook"}],
        render, cfg, tmp_path / "s.ass",
    )
    style_line = next(
        line for line in path.read_text().splitlines() if line.startswith("Style: Hook")
    )
    margin_v = int(style_line.split(",")[-2])
    assert margin_v >= int(render.height * render.safe_top)


def test_empty_cards_are_skipped(tmp_path, render):
    path = subtitles.build_ass(
        [{"start": 0.0, "end": 1.0, "text": "   "}], render, SubtitleSettings(), tmp_path / "s.ass"
    )
    assert "Dialogue:" not in path.read_text()


def test_zero_length_cards_are_skipped(tmp_path, render):
    path = subtitles.build_ass(
        [{"start": 2.0, "end": 2.0, "text": "X"}], render, SubtitleSettings(), tmp_path / "s.ass"
    )
    assert "Dialogue:" not in path.read_text()


def test_short_videos_skip_burn_in():
    cfg = SubtitleSettings(min_duration_s=12.0)
    assert not subtitles.should_burn(cfg, 9.0, True)
    assert subtitles.should_burn(cfg, 20.0, True)
    assert not subtitles.should_burn(cfg, 20.0, False)


def test_srt_sidecar_is_written(tmp_path):
    path = subtitles.write_srt(
        [{"start": 1.5, "end": 3.25, "text": "HELLO"}], tmp_path / "s.srt"
    )
    content = path.read_text()
    assert "00:00:01,500 --> 00:00:03,250" in content
    assert "HELLO" in content


# --------------------------------------------------------------------------- #
# QC
# --------------------------------------------------------------------------- #
def test_missing_file_fails_qc(tmp_path, settings):
    report = check_video(tmp_path / "nope.mp4", 15.0, settings.qc, settings.render)
    assert not report.passed
    assert "does not exist" in report.errors[0]


def test_qc_report_serialises():
    report = QCReport(passed=False, errors=["boom"], warnings=["hmm"])
    assert report.to_dict()["errors"] == ["boom"]
    assert "boom" in report.summary()


# --------------------------------------------------------------------------- #
# Real renders
# --------------------------------------------------------------------------- #
@pytest.mark.ffmpeg
def test_synth_provider_renders_a_real_clip(tmp_path, render):
    from pawparty.providers.video.synth import SynthVideoProvider

    output = tmp_path / "clip.mp4"
    SynthVideoProvider().generate_clip(
        prompt="a kitten dancing under a mirrorball",
        negative_prompt="",
        duration_s=2.0,
        width=540, height=960, fps=24,
        output=output, seed=1,
    )
    info = ffmpeg.probe(output)
    assert info.width == 540 and info.height == 960
    assert 1.7 < info.duration_s < 2.4


@pytest.mark.ffmpeg
def test_synth_music_is_on_tempo_and_the_right_length(tmp_path):
    from pawparty.providers.music.synth import SynthMusicProvider

    result = SynthMusicProvider().get_track(
        prompt="", vibe="disco_funk", tags=["disco", "high"], bpm=120,
        duration_s=3.0, output=tmp_path / "bed.wav", seed=7,
    )
    info = ffmpeg.probe(result.path)
    assert info.has_audio
    assert 2.9 < info.duration_s < 3.2


@pytest.mark.ffmpeg
def test_beats_are_normalised_to_the_target_spec(tmp_path, render):
    from pawparty.media.assemble import prepare_beat
    from pawparty.providers.video.synth import SynthVideoProvider

    raw = tmp_path / "raw.mp4"
    SynthVideoProvider().generate_clip(
        prompt="test", negative_prompt="", duration_s=2.0,
        width=640, height=640, fps=24, output=raw, seed=3,
    )
    beat = Beat(
        index=1, role="hook", duration_s=1.5, action="dance",
        camera="slow_push_in", camera_prompt="push in", transition_in="cut",
    )
    prepared = prepare_beat(raw, beat, render, tmp_path / "prepared.mp4")
    info = ffmpeg.probe(prepared)

    assert info.width == render.width
    assert info.height == render.height
    assert abs(info.duration_s - 1.5) < 0.25


@pytest.mark.ffmpeg
def test_stitching_preserves_total_duration(tmp_path, render):
    """The bug this guards: an xfade offset at the stream end truncates the rest."""
    from pawparty.media.assemble import stitch
    from pawparty.providers.video.synth import SynthVideoProvider

    provider = SynthVideoProvider()
    beats, clips = [], []
    # Deliberately mixes hard cuts with real transitions.
    for index, transition in enumerate(["cut", "cut", "whip_pan", "crossfade"], start=1):
        path = tmp_path / f"b{index}.mp4"
        provider.generate_clip(
            prompt=f"beat {index}", negative_prompt="", duration_s=2.0,
            width=360, height=640, fps=24, output=path, seed=index,
        )
        clips.append(path)
        beats.append(
            Beat(index=index, role="build", duration_s=2.0, action="dance",
                 camera="static", camera_prompt="", transition_in=transition)
        )

    out = stitch(clips, beats, RenderSettings(width=360, height=640, fps=24),
                 tmp_path / "stitched.mp4", tmp_path / "work")
    duration = ffmpeg.probe(out).duration_s

    # 8s of material minus a little for the two real transitions.
    assert 7.0 < duration < 8.3, f"stitched video is {duration:.2f}s, expected ~8s"
