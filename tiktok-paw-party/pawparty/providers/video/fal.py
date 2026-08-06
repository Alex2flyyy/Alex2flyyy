"""fal.ai video provider.

fal exposes models at ``https://fal.run/<model-id>`` (synchronous) and
``https://queue.fal.run/<model-id>`` (queued). We use the queue API because
video jobs regularly exceed any sane HTTP timeout.

Configure in `config/settings.yaml`::

    providers:
      video: fal
      options:
        fal:
          model: "fal-ai/<model-id>"
          cost_per_second_usd: 0.06
          input_map:
            prompt: prompt
            duration: duration
            first_frame: image_url
          static_input:
            aspect_ratio: "9:16"

Requires ``FAL_KEY``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...logging_setup import get_logger
from ..base import BaseProvider, GenerationResult, ProviderError, ProviderUnavailable
from ..http import download, first_url, poll_until, request_json

log = get_logger("providers.fal")

QUEUE_ROOT = "https://queue.fal.run"

_DEFAULT_INPUT_MAP = {
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
    "duration": "duration",
    "seed": "seed",
    "first_frame": "image_url",
}


class FalVideoProvider(BaseProvider):
    name = "fal"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.key = os.getenv("FAL_KEY", "").strip()
        self.model = str(self.opt("model", "")).strip()
        self.cost_per_second = float(self.opt("cost_per_second_usd", 0.06))
        self.input_map = {**_DEFAULT_INPUT_MAP, **(self.opt("input_map") or {})}
        self.static_input = dict(self.opt("static_input") or {})
        self.timeout_s = int(self.opt("timeout_s", 900))

    def estimate_cost(self, duration_s: float) -> float:
        return round(self.cost_per_second * max(1.0, duration_s), 4)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self.key}", "Content-Type": "application/json"}

    def _check(self) -> None:
        if not self.key:
            raise ProviderUnavailable(
                "FAL_KEY is not set. Add it to .env or switch providers.video to 'synth'."
            )
        if not self.model:
            raise ProviderUnavailable("providers.options.fal.model is not set.")

    # ------------------------------------------------------------------ #
    def generate_clip(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        duration_s: float,
        width: int,   # noqa: ARG002 - fal models take aspect_ratio, not pixels
        height: int,  # noqa: ARG002
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
            payload[self.input_map["duration"]] = int(round(duration_s))
        if self.input_map.get("seed"):
            payload[self.input_map["seed"]] = int(seed) % (2**31)
        if self.input_map.get("fps"):
            payload[self.input_map["fps"]] = fps
        if first_frame and self.input_map.get("first_frame"):
            payload[self.input_map["first_frame"]] = _to_data_uri(Path(first_frame))

        submitted = request_json(
            "POST", f"{QUEUE_ROOT}/{self.model}",
            headers=self._headers, json_body=payload,
        )
        status_url = submitted.get("status_url") or f"{QUEUE_ROOT}/{self.model}/requests/{submitted.get('request_id')}/status"
        response_url = submitted.get("response_url") or f"{QUEUE_ROOT}/{self.model}/requests/{submitted.get('request_id')}"

        poll_until(
            lambda: request_json("GET", status_url, headers=self._headers),
            is_done=lambda p: p.get("status") == "COMPLETED",
            is_failed=lambda p: p.get("status") in ("FAILED", "CANCELLED", "ERROR"),
            timeout_s=self.timeout_s,
            label=f"fal request {submitted.get('request_id')}",
        )
        result = request_json("GET", response_url, headers=self._headers)

        url = first_url(result)
        if not url:
            raise ProviderError(f"fal returned no downloadable output: {str(result)[:300]}")
        download(url, output)

        return GenerationResult(
            path=output,
            cost_usd=self.estimate_cost(duration_s),
            provider=self.name,
            model=self.model,
            duration_s=duration_s,
            metadata={"request_id": submitted.get("request_id")},
        )


def _to_data_uri(path: Path) -> str:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
