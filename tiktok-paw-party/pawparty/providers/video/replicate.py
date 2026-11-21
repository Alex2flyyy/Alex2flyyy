"""Replicate video provider.

Replicate exposes hundreds of video models behind one prediction API, so this
one integration covers whatever the current best text-to-video or
image-to-video model happens to be. **You choose the model in config** — this
file deliberately hard-codes no model slug, because the good ones change every
few months and a pinned slug would rot.

Configure in `config/settings.yaml`::

    providers:
      video: replicate
      options:
        replicate:
          model: "owner/model-name"        # or "owner/model:versionhash"
          cost_per_second_usd: 0.05        # from the model's pricing page
          input_map:                       # maps our fields -> model fields
            prompt: prompt
            negative_prompt: negative_prompt
            duration: duration
            aspect_ratio: aspect_ratio
            seed: seed
            first_frame: start_image
          static_input:                    # anything else the model wants
            aspect_ratio: "9:16"

Requires ``REPLICATE_API_TOKEN``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...logging_setup import get_logger
from ..base import BaseProvider, GenerationResult, ProviderError, ProviderUnavailable
from ..http import download, first_url, poll_until, request_json

log = get_logger("providers.replicate")

API_ROOT = "https://api.replicate.com/v1"

_DEFAULT_INPUT_MAP = {
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
    "duration": "duration",
    "seed": "seed",
    "first_frame": "image",
}


class ReplicateVideoProvider(BaseProvider):
    name = "replicate"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.token = os.getenv("REPLICATE_API_TOKEN", "").strip()
        self.model = str(self.opt("model", "")).strip()
        self.cost_per_second = float(self.opt("cost_per_second_usd", 0.05))
        self.input_map = {**_DEFAULT_INPUT_MAP, **(self.opt("input_map") or {})}
        self.static_input = dict(self.opt("static_input") or {})
        self.timeout_s = int(self.opt("timeout_s", 900))
        #: Clip lengths this model accepts, ascending. Varies by model, so it is
        #: configurable; `pawparty wire --show-schema` prints the real enum.
        blocks = self.opt("duration_blocks") or [5, 6, 8, 10]
        self.duration_blocks = sorted({int(b) for b in blocks if int(b) > 0}) or [10]
        self._warned_over_limit = False

    # ------------------------------------------------------------------ #
    def billed_seconds(self, duration_s: float) -> float:
        """Replicate bills the whole requested block, not the trimmed result."""
        return float(self._round_duration(duration_s))

    def exceeds_model_limit(self, duration_s: float) -> bool:
        """True when a beat is longer than this model can generate in one clip.

        Such a beat is still *requested* at the largest block — there is no
        other option — and the assembly stage then retimes the short clip to
        fill the beat, which softens the motion. Worth knowing before a paid
        run rather than after.
        """
        return duration_s > max(self.duration_blocks)

    def estimate_cost(self, duration_s: float) -> float:
        return round(self.cost_per_second * self.billed_seconds(duration_s), 4)

    def _check(self) -> None:
        if not self.token:
            raise ProviderUnavailable(
                "REPLICATE_API_TOKEN is not set. Add it to .env or switch "
                "providers.video to 'synth' in config/settings.yaml."
            )
        if not self.model:
            raise ProviderUnavailable(
                "providers.options.replicate.model is not set. Pick a video model "
                "at https://replicate.com/collections/text-to-video and set its slug."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }

    # ------------------------------------------------------------------ #
    def generate_clip(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        duration_s: float,
        width: int,
        height: int,
        fps: int,
        output: Path,
        seed: int,
        first_frame: Path | None = None,
    ) -> GenerationResult:
        self._check()

        payload: dict[str, Any] = dict(self.static_input)
        payload[self.input_map["prompt"]] = prompt
        if self.input_map.get("negative_prompt"):
            payload[self.input_map["negative_prompt"]] = negative_prompt
        if self.input_map.get("duration"):
            payload[self.input_map["duration"]] = self._round_duration(duration_s)
        if self.input_map.get("seed"):
            payload[self.input_map["seed"]] = int(seed) % (2**31)
        if self.input_map.get("width"):
            payload[self.input_map["width"]] = width
        if self.input_map.get("height"):
            payload[self.input_map["height"]] = height
        if self.input_map.get("fps"):
            payload[self.input_map["fps"]] = fps
        if first_frame and self.input_map.get("first_frame"):
            # Replicate accepts a data URI or a public URL for image inputs.
            payload[self.input_map["first_frame"]] = _to_data_uri(Path(first_frame))

        prediction = self._create_prediction(payload)
        final = poll_until(
            lambda: request_json("GET", f"{API_ROOT}/predictions/{prediction['id']}",
                                 headers=self._headers),
            is_done=lambda p: p.get("status") == "succeeded",
            is_failed=lambda p: p.get("status") in ("failed", "canceled"),
            timeout_s=self.timeout_s,
            label=f"replicate prediction {prediction['id']}",
        )

        url = first_url(final.get("output"))
        if not url:
            raise ProviderError(
                f"Replicate returned no downloadable output: {str(final.get('output'))[:300]}"
            )
        download(url, output)

        return GenerationResult(
            path=output,
            cost_usd=self.estimate_cost(duration_s),
            provider=self.name,
            model=self.model,
            duration_s=duration_s,
            metadata={"prediction_id": final.get("id"), "metrics": final.get("metrics", {})},
        )

    # ------------------------------------------------------------------ #
    def _create_prediction(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle both `owner/model` and `owner/model:version` addressing."""
        if ":" in self.model:
            _, version = self.model.split(":", 1)
            return request_json(
                "POST", f"{API_ROOT}/predictions",
                headers=self._headers,
                json_body={"version": version, "input": payload},
            )
        return request_json(
            "POST", f"{API_ROOT}/models/{self.model}/predictions",
            headers=self._headers,
            json_body={"input": payload},
        )

    def _round_duration(self, duration_s: float) -> int:
        """Round a beat length up to the nearest duration the model accepts.

        Models only take a few discrete lengths, so we ask for the nearest
        supported value and let the assembly stage trim to the exact beat
        duration — trimming is free, re-generating is not. This is also the
        billed block, so :meth:`billed_seconds` reuses it.

        A beat longer than the largest block cannot be generated in one clip.
        We clamp to the largest block (the only thing the API would accept) but
        warn, because the result is a short clip that assembly has to retime —
        a quality cost that is otherwise invisible until you watch the output.
        The blocks are configurable per model; the default is the set used by
        most current video models.
        """
        blocks = self.duration_blocks
        for allowed in blocks:
            if duration_s <= allowed:
                return allowed

        largest = blocks[-1]
        if not self._warned_over_limit:
            self._warned_over_limit = True
            log.warning(
                "beat of %.2fs exceeds the longest clip %s can generate (%ss). "
                "Requesting %ss and letting assembly retime it, which softens the "
                "motion. Either lower the duration band's ceiling, or set "
                "providers.options.replicate.duration_blocks if this model "
                "supports longer clips.",
                duration_s, self.model or "this model", largest, largest,
            )
        return largest


def _to_data_uri(path: Path) -> str:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
