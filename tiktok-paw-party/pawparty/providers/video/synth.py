"""`synth` video provider — offline, free, no credentials.

This does **not** produce cute animals. It produces a real, correctly-timed,
correctly-sized MP4 with animated colour fields, motion, and the beat's prompt
burned in. That matters for three reasons:

1. You can run and test the entire pipeline — timing, transitions, music mix,
   subtitle placement, QC, scheduling — before spending a cent on generation.
2. CI can verify rendering without network access or secrets.
3. When a paid provider is down mid-batch, the pipeline can degrade to `synth`
   and still hand you a structurally complete video to inspect, rather than a
   stack trace at 3am.

Switch to a real provider by changing one line in `config/settings.yaml`.
"""

from __future__ import annotations

import colorsys
from pathlib import Path

from ...media import ffmpeg
from ...util import seeded_rng, short_hash
from ..base import BaseProvider, GenerationResult

#: A small palette of pleasant, high-saturation hues so previews stay readable.
_HUE_STEPS = 12


class SynthVideoProvider(BaseProvider):
    """Renders a placeholder clip with ffmpeg's built-in sources."""

    name = "synth"

    def estimate_cost(self, duration_s: float) -> float:  # noqa: ARG002 - free
        return 0.0

    def generate_clip(
        self,
        *,
        prompt: str,
        negative_prompt: str,  # noqa: ARG002 - not meaningful offline
        duration_s: float,
        width: int,
        height: int,
        fps: int,
        output: Path,
        seed: int,
        first_frame: Path | None = None,
    ) -> GenerationResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        rng = seeded_rng(seed, prompt[:64])

        if first_frame and Path(first_frame).exists():
            args = self._animate_still(
                Path(first_frame), duration_s, width, height, fps, output, rng.random()
            )
        else:
            args = self._synthesize(prompt, duration_s, width, height, fps, output, rng)

        ffmpeg.run(args, label=f"synth clip {output.name}", timeout=300)
        return GenerationResult(
            path=output,
            cost_usd=0.0,
            provider=self.name,
            model="ffmpeg-synth",
            duration_s=duration_s,
            metadata={"placeholder": True, "prompt_digest": short_hash(prompt)},
        )

    # ------------------------------------------------------------------ #
    def _synthesize(
        self,
        prompt: str,
        duration_s: float,
        width: int,
        height: int,
        fps: int,
        output: Path,
        rng,
    ) -> list[str]:
        """Gradient + moving blobs + drifting text, all from lavfi sources."""
        hue = rng.randrange(_HUE_STEPS) / _HUE_STEPS
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.72, 0.98))
        r2, g2, b2 = (
            int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.42) % 1.0, 0.80, 0.92)
        )
        # ffmpeg's `gradients` filter accepts speed in (0, 1]; anything above
        # that fails the whole render with "Numerical result out of range".
        speed = 0.15 + rng.random() * 0.8
        label = _wrap(prompt, 34)[:3]
        drawtexts = self._drawtext_stack(label, height, fps)

        # `gradients` gives a clean animated backdrop; `life` adds organic
        # movement on top so motion-detection style QC sees real change.
        return [
            "-f", "lavfi",
            "-i",
            f"gradients=s={width}x{height}:c0=0x{r:02x}{g:02x}{b:02x}:"
            f"c1=0x{r2:02x}{g2:02x}{b2:02x}:x0={width//4}:y0={height//4}:"
            f"x1={width}:y1={height}:speed={speed:.3f}:d={duration_s:.3f}:r={fps}",
            "-f", "lavfi",
            "-i",
            f"life=s={max(64, width // 8)}x{max(64, height // 8)}:mold=10:r={fps}:"
            f"ratio=0.11:death_color=#101820:life_color=#ffffff:seed={rng.randrange(1 << 30)}",
            "-filter_complex",
            (
                f"[1:v]scale={width}:{height},format=gray,"
                f"boxblur=18:2,colorchannelmixer=aa=0.22[tex];"
                f"[0:v][tex]overlay=0:0:format=auto,"
                f"{drawtexts},format=yuv420p[v]"
            ),
            "-map", "[v]",
            "-t", f"{duration_s:.3f}",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            str(output),
        ]

    def _drawtext_stack(self, lines: list[str], height: int, fps: int) -> str:
        """Stacked, gently bobbing text so preview clips are self-describing."""
        if not lines:
            return "null"
        parts = []
        base_y = height // 2 - (len(lines) * 60) // 2
        for i, line in enumerate(lines):
            text = ffmpeg.escape_filter_text(line)
            parts.append(
                f"drawtext=text='{text}':fontcolor=white@0.92:fontsize=46:"
                f"box=1:boxcolor=black@0.35:boxborderw=14:"
                f"x=(w-text_w)/2:y={base_y + i * 74}+12*sin(t*2.4+{i})"
            )
        parts.append(
            "drawtext=text='PREVIEW \\- synth provider':fontcolor=white@0.55:fontsize=28:"
            "x=(w-text_w)/2:y=h-160"
        )
        return ",".join(parts)

    def _animate_still(
        self,
        still: Path,
        duration_s: float,
        width: int,
        height: int,
        fps: int,
        output: Path,
        phase: float,
    ) -> list[str]:
        """Ken Burns a provided first frame — mirrors the image-to-video path."""
        frames = max(2, int(duration_s * fps))
        zoom_end = 1.10 + phase * 0.08
        return [
            "-loop", "1", "-i", str(still),
            "-vf",
            (
                f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
                f"crop={width * 2}:{height * 2},"
                f"zoompan=z='min(zoom+{(zoom_end - 1) / frames:.6f},{zoom_end:.3f})':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s={width}x{height}:fps={fps},"
                f"format=yuv420p"
            ),
            "-t", f"{duration_s:.3f}",
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            str(output),
        ]


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            if current:
                lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
