"""ASS subtitle generation for burned-in on-screen text.

ASS rather than SRT because SRT can't position or animate text, and placement is
the whole point: TikTok's own UI covers the bottom ~20% and the top ~12% of the
frame, so unpositioned captions land under the share button.

Each card gets a scale-pop entrance (``\\t`` transform), which is the small
detail that separates "text on a video" from "edited".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..config import RenderSettings, SubtitleSettings

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{hook_size},{primary},{primary},{outline_colour},&H64000000,-1,0,0,0,100,100,1,0,1,{outline},{shadow},8,60,60,{margin_top},1
Style: Beat,{font},{body_size},{primary},{primary},{outline_colour},&H64000000,-1,0,0,0,100,100,1,0,1,{outline},{shadow},8,60,60,{margin_top},1
Style: CTA,{font},{body_size},{primary},{primary},{outline_colour},&H64000000,-1,0,0,0,100,100,1,0,1,{outline},{shadow},2,60,60,{margin_bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

#: emphasis -> (style name, entrance transform)
_STYLES = {
    "hook": ("Hook", r"{\fscx70\fscy70\t(0,140,\fscx104\fscy104)\t(140,220,\fscx100\fscy100)}"),
    "beat": ("Beat", r"{\fscx88\fscy88\t(0,120,\fscx100\fscy100)}"),
    "cta": ("CTA", r"{\fad(160,120)}"),
}


def _timestamp(seconds: float) -> str:
    """ASS uses `H:MM:SS.cc` (centiseconds, single-digit hours)."""
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


def build_ass(
    lines: Sequence[dict[str, Any]],
    render: RenderSettings,
    subtitle_cfg: SubtitleSettings,
    output: Path,
) -> Path:
    """Write an ASS file for the given timed cards.

    Cards are placed inside the safe area computed from ``render.safe_top`` /
    ``render.safe_bottom``, so text never collides with platform chrome.
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    margin_top = int(render.height * render.safe_top)
    margin_bottom = int(render.height * render.safe_bottom)

    header = _HEADER.format(
        width=render.width,
        height=render.height,
        font=subtitle_cfg.font,
        hook_size=subtitle_cfg.font_size,
        body_size=int(subtitle_cfg.font_size * 0.82),
        primary=subtitle_cfg.primary_color,
        outline_colour=subtitle_cfg.outline_color,
        outline=subtitle_cfg.outline,
        shadow=subtitle_cfg.shadow,
        margin_top=margin_top,
        margin_bottom=margin_bottom,
    )

    events: list[str] = []
    for card in lines:
        text = str(card.get("text", "")).strip()
        if not text:
            continue
        start = float(card.get("start", 0.0))
        end = float(card.get("end", start + 2.0))
        if end <= start:
            continue
        style_name, transform = _STYLES.get(str(card.get("emphasis", "beat")), _STYLES["beat"])
        events.append(
            f"Dialogue: 0,{_timestamp(start)},{_timestamp(end)},{style_name},,0,0,0,,"
            f"{transform}{_escape(text)}"
        )

    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output


def force_style(subtitle_cfg: SubtitleSettings) -> str:
    """A `force_style` string for ffmpeg's `subtitles` filter.

    Only needed as a safety net when the ASS styles can't be honoured (e.g. the
    named font is missing on the render host).
    """
    return (
        f"FontName={subtitle_cfg.font},"
        f"Outline={subtitle_cfg.outline},"
        f"Shadow={subtitle_cfg.shadow}"
    )


def should_burn(subtitle_cfg: SubtitleSettings, duration_s: float, has_lines: bool) -> bool:
    """Short clips read better clean — the hook alone carries them."""
    return bool(subtitle_cfg.enabled and has_lines and duration_s >= subtitle_cfg.min_duration_s)


def write_srt(lines: Sequence[dict[str, Any]], output: Path) -> Path:
    """Also emit an SRT sidecar.

    TikTok ignores it, but it is useful for cross-posting to YouTube Shorts and
    for keeping a plain-text record of what was on screen.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for index, card in enumerate(lines, start=1):
        text = str(card.get("text", "")).strip()
        if not text:
            continue
        start = _srt_timestamp(float(card.get("start", 0.0)))
        end = _srt_timestamp(float(card.get("end", 0.0)))
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    output.write_text("\n".join(blocks), encoding="utf-8")
    return output


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
