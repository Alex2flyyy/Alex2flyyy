"""Provider interfaces.

Everything that costs money or talks to a third party sits behind one of these
four Protocols. The pipeline never imports a vendor SDK directly, so swapping
Replicate for fal.ai — or dropping in a model that doesn't exist yet — is a
config change plus one new file in ``providers/``.

Each provider reports an estimated cost so the budget guard can stop a runaway
batch *before* the spend, not after.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Any provider-side failure. Retried by the pipeline where sensible."""


class ProviderUnavailable(ProviderError):
    """Provider is configured but unusable (missing key, no network, no quota).

    Distinct from :class:`ProviderError` because the pipeline degrades to the
    offline `synth` provider on this rather than failing the whole run.
    """


@dataclass
class GenerationResult:
    """What every generate call returns."""

    path: Path
    cost_usd: float = 0.0
    provider: str = ""
    model: str = ""
    duration_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VideoProvider(Protocol):
    """Generates a single video clip for one beat."""

    name: str

    def estimate_cost(self, duration_s: float) -> float:
        """USD estimate for one clip of this length. Used by the budget guard."""

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
        """Write a clip to ``output`` and return its result descriptor."""


@runtime_checkable
class ImageProvider(Protocol):
    """Generates a still — used as a first frame, a cover, or a thumbnail."""

    name: str

    def estimate_cost(self) -> float: ...

    def generate_image(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        output: Path,
        seed: int,
    ) -> GenerationResult: ...


@runtime_checkable
class MusicProvider(Protocol):
    """Supplies a music bed of at least the requested duration."""

    name: str

    def estimate_cost(self, duration_s: float) -> float: ...

    def get_track(
        self,
        *,
        prompt: str,
        vibe: str,
        tags: list[str],
        bpm: int,
        duration_s: float,
        output: Path,
        seed: int,
    ) -> GenerationResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Writes captions/hooks. Optional — the offline writer covers the basics."""

    name: str

    def estimate_cost(self) -> float: ...

    def write_copy(self, prompt: str) -> dict[str, Any]:
        """Return the JSON object described in ``copy_llm.j2``."""


class BaseProvider:
    """Shared plumbing: name, options, and a uniform ``__repr__``."""

    name = "base"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def opt(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.options or self.options[key] in (None, ""):
            raise ProviderUnavailable(
                f"Provider {self.name!r} requires option {key!r}. "
                f"Set it under providers.options.{self.name} in config/settings.yaml."
            )
        return self.options[key]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
