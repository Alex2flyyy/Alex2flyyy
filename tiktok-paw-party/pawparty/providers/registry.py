"""Provider registry — name -> class, plus construction and fallback policy.

Adding a provider is two steps:

1. Write a class implementing the relevant Protocol in ``providers/<kind>/``.
2. Add one line to the matching dict below.

Nothing else in the codebase needs to know it exists.

Fallback policy: if a configured provider raises
:class:`~pawparty.providers.base.ProviderUnavailable` at construction time (no
key, no model configured), the registry logs a loud warning and substitutes the
offline `synth` provider. A missing credential should downgrade the night's
output to previews, not silently produce nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import Settings
from ..logging_setup import get_logger
from ..storage import Workspace
from .base import ProviderUnavailable
from .image.replicate import ReplicateImageProvider
from .image.synth import SynthImageProvider
from .llm.anthropic import AnthropicLLMProvider
from .llm.offline import OfflineLLMProvider
from .music.local_library import LocalLibraryMusicProvider
from .music.synth import SynthMusicProvider
from .video.fal import FalVideoProvider
from .video.replicate import ReplicateVideoProvider
from .video.synth import SynthVideoProvider

log = get_logger("providers.registry")

VIDEO_PROVIDERS: dict[str, Callable[..., Any]] = {
    "synth": SynthVideoProvider,
    "replicate": ReplicateVideoProvider,
    "fal": FalVideoProvider,
}

IMAGE_PROVIDERS: dict[str, Callable[..., Any]] = {
    "synth": SynthImageProvider,
    "replicate": ReplicateImageProvider,
}

MUSIC_PROVIDERS: dict[str, Callable[..., Any]] = {
    "synth": SynthMusicProvider,
    "local_library": LocalLibraryMusicProvider,
}

LLM_PROVIDERS: dict[str, Callable[..., Any]] = {
    "offline": OfflineLLMProvider,
    "anthropic": AnthropicLLMProvider,
}


@dataclass
class ProviderBundle:
    """The four providers a pipeline run needs, already constructed."""

    video: Any
    image: Any
    music: Any
    llm: Any
    degraded: list[str]

    @property
    def is_preview_only(self) -> bool:
        """True when video is coming from `synth` — i.e. nothing publishable."""
        return getattr(self.video, "name", "") == "synth"

    def describe(self) -> dict[str, str]:
        return {
            "video": getattr(self.video, "name", "?"),
            "image": getattr(self.image, "name", "?"),
            "music": getattr(self.music, "name", "?"),
            "llm": getattr(self.llm, "name", "?"),
        }


def _construct(
    kind: str,
    name: str,
    table: dict[str, Callable[..., Any]],
    options: dict[str, Any],
    fallback: str,
    degraded: list[str],
) -> Any:
    if name not in table:
        raise KeyError(
            f"Unknown {kind} provider {name!r}. Available: {', '.join(sorted(table))}"
        )
    try:
        provider = table[name](options)
        # Providers validate credentials lazily; ask them to check now so a
        # missing key surfaces before we've rendered half a batch.
        check = getattr(provider, "_check", None)
        if callable(check):
            check()
        return provider
    except ProviderUnavailable as exc:
        log.warning(
            "%s provider %r unavailable (%s) — falling back to %r",
            kind, name, exc, fallback,
        )
        degraded.append(f"{kind}:{name}->{fallback}")
        return table[fallback]({})


def build_providers(settings: Settings, workspace: Workspace) -> ProviderBundle:
    """Construct every provider named in settings, applying fallbacks."""
    cfg = settings.providers
    degraded: list[str] = []

    video = _construct("video", cfg.video, VIDEO_PROVIDERS, cfg.opts(cfg.video), "synth", degraded)

    # The image provider's options live under its own key when it is Replicate,
    # so image and video can point at different models on the same account.
    image_opts = cfg.opts("replicate_image") if cfg.image == "replicate" else cfg.opts(cfg.image)
    image = _construct("image", cfg.image, IMAGE_PROVIDERS, image_opts, "synth", degraded)

    music = _construct("music", cfg.music, MUSIC_PROVIDERS, cfg.opts(cfg.music), "synth", degraded)
    if isinstance(music, LocalLibraryMusicProvider):
        music.bind_library(workspace.audio_library)

    llm = _construct("llm", cfg.llm, LLM_PROVIDERS, cfg.opts(cfg.llm), "offline", degraded)

    bundle = ProviderBundle(video=video, image=image, music=music, llm=llm, degraded=degraded)
    log.info("providers: %s", bundle.describe())
    return bundle
