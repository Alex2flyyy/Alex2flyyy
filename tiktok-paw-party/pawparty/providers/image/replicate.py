"""Replicate image provider — first frames and cover stills.

Same configuration shape as the video provider; see that module's docstring.

    providers:
      image: replicate
      options:
        replicate_image:
          model: "owner/model-name"
          cost_per_image_usd: 0.01
          static_input:
            aspect_ratio: "9:16"
            output_format: "png"
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..base import BaseProvider, GenerationResult, ProviderError, ProviderUnavailable
from ..http import download, first_url, poll_until, request_json

API_ROOT = "https://api.replicate.com/v1"


class ReplicateImageProvider(BaseProvider):
    name = "replicate_image"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.token = os.getenv("REPLICATE_API_TOKEN", "").strip()
        self.model = str(self.opt("model", "")).strip()
        self.cost_per_image = float(self.opt("cost_per_image_usd", 0.01))
        self.static_input = dict(self.opt("static_input") or {})
        self.input_map = {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            **(self.opt("input_map") or {}),
        }
        self.timeout_s = int(self.opt("timeout_s", 300))

    def estimate_cost(self) -> float:
        return self.cost_per_image

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }

    def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        output: Path,
        seed: int,
    ) -> GenerationResult:
        if not self.token:
            raise ProviderUnavailable("REPLICATE_API_TOKEN is not set.")
        if not self.model:
            raise ProviderUnavailable("providers.options.replicate_image.model is not set.")

        payload: dict[str, Any] = dict(self.static_input)
        payload[self.input_map["prompt"]] = prompt
        if self.input_map.get("negative_prompt"):
            payload[self.input_map["negative_prompt"]] = negative_prompt
        if self.input_map.get("seed"):
            payload[self.input_map["seed"]] = int(seed) % (2**31)
        if self.input_map.get("width"):
            payload[self.input_map["width"]] = width
        if self.input_map.get("height"):
            payload[self.input_map["height"]] = height

        if ":" in self.model:
            _, version = self.model.split(":", 1)
            created = request_json(
                "POST", f"{API_ROOT}/predictions",
                headers=self._headers, json_body={"version": version, "input": payload},
            )
        else:
            created = request_json(
                "POST", f"{API_ROOT}/models/{self.model}/predictions",
                headers=self._headers, json_body={"input": payload},
            )

        final = poll_until(
            lambda: request_json("GET", f"{API_ROOT}/predictions/{created['id']}",
                                 headers=self._headers),
            is_done=lambda p: p.get("status") == "succeeded",
            is_failed=lambda p: p.get("status") in ("failed", "canceled"),
            timeout_s=self.timeout_s,
            label=f"replicate image {created['id']}",
        )

        url = first_url(final.get("output"))
        if not url:
            raise ProviderError("Replicate image prediction returned no output URL")
        download(url, output)
        return GenerationResult(
            path=output,
            cost_usd=self.cost_per_image,
            provider=self.name,
            model=self.model,
            metadata={"prediction_id": final.get("id")},
        )
