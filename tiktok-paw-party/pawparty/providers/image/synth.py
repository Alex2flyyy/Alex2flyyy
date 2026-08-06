"""`synth` image provider — offline placeholder stills (covers, first frames)."""

from __future__ import annotations

import colorsys
from pathlib import Path

from ...media import ffmpeg
from ...util import seeded_rng, short_hash
from ..base import BaseProvider, GenerationResult


class SynthImageProvider(BaseProvider):
    name = "synth"

    def estimate_cost(self) -> float:
        return 0.0

    def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: str,  # noqa: ARG002
        width: int,
        height: int,
        output: Path,
        seed: int,
    ) -> GenerationResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        rng = seeded_rng(seed, prompt[:64])
        hue = rng.random()
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.7, 0.97))
        r2, g2, b2 = (int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.5) % 1.0, 0.75, 0.9))

        lines = _wrap(prompt, 30)[:4]
        text_filters = []
        base_y = height // 2 - len(lines) * 40
        for i, line in enumerate(lines):
            escaped = ffmpeg.escape_filter_text(line)
            text_filters.append(
                f"drawtext=text='{escaped}':fontcolor=white:fontsize=44:"
                f"box=1:boxcolor=black@0.4:boxborderw=12:"
                f"x=(w-text_w)/2:y={base_y + i * 72}"
            )
        chain = ",".join(text_filters) if text_filters else "null"

        ffmpeg.run(
            [
                "-f", "lavfi",
                "-i",
                f"gradients=s={width}x{height}:c0=0x{r:02x}{g:02x}{b:02x}:"
                f"c1=0x{r2:02x}{g2:02x}{b2:02x}:d=1:r=1",
                "-frames:v", "1",
                "-vf", chain,
                str(output),
            ],
            label=f"synth image {output.name}",
            timeout=120,
        )
        return GenerationResult(
            path=output,
            cost_usd=0.0,
            provider=self.name,
            model="ffmpeg-synth",
            metadata={"placeholder": True, "prompt_digest": short_hash(prompt)},
        )


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
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
