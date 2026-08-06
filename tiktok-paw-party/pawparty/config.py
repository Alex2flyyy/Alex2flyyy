"""Configuration loading.

Two layers, both plain YAML so a non-programmer can tune the account without
touching Python:

* ``config/settings.yaml``  — how the machine runs (render specs, providers,
  budgets, schedule, QC thresholds). Shared by every channel.
* ``config/channels/<name>.yaml`` — what the channel *is* (its vocabulary of
  animals, dance styles, settings, accessories, music vibes, caption voice).
  Adding a new faceless channel = adding one file here. No code changes.

Environment variables always win over YAML for secrets and for the handful of
operational knobs listed in ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is a convenience, not a requirement
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - trivial fallback
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


class ConfigError(RuntimeError):
    """Raised for missing files, unknown channels, or invalid values."""


# --------------------------------------------------------------------------- #
# Typed sections
# --------------------------------------------------------------------------- #
@dataclass
class RenderSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    video_codec: str = "libx264"
    crf: int = 20
    preset: str = "medium"
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "160k"
    audio_sample_rate: int = 48000
    loudness_lufs: float = -14.0
    #: Safe margin (fraction of height) kept clear of TikTok's own UI overlays.
    safe_top: float = 0.12
    safe_bottom: float = 0.20


@dataclass
class ProviderSettings:
    video: str = "synth"
    image: str = "synth"
    music: str = "synth"
    llm: str = "offline"
    options: dict[str, dict[str, Any]] = field(default_factory=dict)

    def opts(self, name: str) -> dict[str, Any]:
        return dict(self.options.get(name, {}))


@dataclass
class BudgetSettings:
    daily_usd: float = 6.0
    per_video_usd: float = 1.25
    abort_on_exceed: bool = True


@dataclass
class QCSettings:
    min_duration_s: float = 8.0
    max_duration_s: float = 60.0
    duration_tolerance_s: float = 1.5
    require_audio: bool = True
    min_file_size_kb: int = 200
    max_file_size_mb: int = 280
    expect_width: int = 1080
    expect_height: int = 1920


@dataclass
class ScheduleSettings:
    videos_per_day: int = 7
    #: Local wall-clock post times. Tuned for US-heavy audiences; adjust after
    #: your own analytics land (see docs/STRATEGY.md).
    post_times: list[str] = field(
        default_factory=lambda: ["07:15", "09:30", "12:00", "15:30", "18:00", "20:15", "22:00"]
    )
    jitter_minutes: int = 12
    timezone: str = "America/Los_Angeles"


@dataclass
class SubtitleSettings:
    enabled: bool = True
    #: Skip burn-in below this duration — very short loops read better clean.
    min_duration_s: float = 12.0
    font: str = "DejaVu Sans"
    font_size: int = 78
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 5
    shadow: int = 2
    margin_v: int = 420


@dataclass
class Settings:
    """Root settings object, loaded once per process."""

    workspace: Path
    project_root: Path
    default_channel: str = "kittens_puppies"
    concurrency: int = 2
    render: RenderSettings = field(default_factory=RenderSettings)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    qc: QCSettings = field(default_factory=QCSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    subtitles: SubtitleSettings = field(default_factory=SubtitleSettings)
    #: Keep intermediates around for debugging; set False to save disk.
    keep_intermediates: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def channels_dir(self) -> Path:
        return self.project_root / "config" / "channels"

    def channel_path(self, name: str) -> Path:
        path = self.channels_dir / f"{name}.yaml"
        if not path.exists():
            available = sorted(p.stem for p in self.channels_dir.glob("*.yaml"))
            raise ConfigError(
                f"Unknown channel {name!r}. Available: {', '.join(available) or '(none)'}"
            )
        return path


# --------------------------------------------------------------------------- #
# Channel profile
# --------------------------------------------------------------------------- #
@dataclass
class ChannelProfile:
    """A channel's creative vocabulary + voice. Pure data, no behaviour."""

    name: str
    display_name: str
    description: str
    vocabulary: dict[str, list[dict[str, Any]]]
    structure: dict[str, Any]
    copy: dict[str, Any]
    hashtags: dict[str, Any]
    prompt_style: dict[str, Any]
    safety: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def pool(self, key: str) -> list[dict[str, Any]]:
        if key not in self.vocabulary:
            raise ConfigError(
                f"Channel {self.name!r} has no vocabulary pool {key!r}. "
                f"Known pools: {', '.join(sorted(self.vocabulary))}"
            )
        return self.vocabulary[key]

    def has_pool(self, key: str) -> bool:
        return key in self.vocabulary and bool(self.vocabulary[key])


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a YAML mapping at top level of {path}")
    return data


def _build(section: type, data: dict[str, Any], where: str) -> Any:
    """Instantiate a dataclass from a YAML mapping, rejecting unknown keys."""
    known = {f.name for f in section.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown key(s) under {where}: {', '.join(sorted(unknown))}")
    return section(**data)


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward looking for `config/settings.yaml`."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "settings.yaml").exists():
            return candidate
    # Fall back to the package's own parent (works for an editable install).
    return Path(__file__).resolve().parent.parent


def load_settings(
    config_path: Path | str | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Load `settings.yaml`, apply env vars, then apply CLI ``overrides``."""
    load_dotenv(override=False)

    if config_path is not None:
        path = Path(config_path).expanduser().resolve()
        project_root = path.parent.parent
    else:
        project_root = find_project_root()
        path = project_root / "config" / "settings.yaml"

    data = _read_yaml(path)
    if overrides:
        data = _deep_merge(data, overrides)

    workspace_raw = os.getenv("PAWPARTY_WORKSPACE") or data.get("workspace") or "Videos"
    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_absolute():
        workspace = (project_root / workspace).resolve()

    budget_data = dict(data.get("budget", {}))
    env_budget = os.getenv("PAWPARTY_DAILY_BUDGET_USD")
    if env_budget:
        try:
            budget_data["daily_usd"] = float(env_budget)
        except ValueError as exc:
            raise ConfigError(f"PAWPARTY_DAILY_BUDGET_USD must be a number, got {env_budget!r}") from exc

    settings = Settings(
        workspace=workspace,
        project_root=project_root,
        default_channel=data.get("default_channel", "kittens_puppies"),
        concurrency=int(data.get("concurrency", 2)),
        render=_build(RenderSettings, data.get("render", {}), "render"),
        providers=_build(ProviderSettings, data.get("providers", {}), "providers"),
        budget=_build(BudgetSettings, budget_data, "budget"),
        qc=_build(QCSettings, data.get("qc", {}), "qc"),
        schedule=_build(ScheduleSettings, data.get("schedule", {}), "schedule"),
        subtitles=_build(SubtitleSettings, data.get("subtitles", {}), "subtitles"),
        keep_intermediates=bool(data.get("keep_intermediates", True)),
        raw=data,
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    render = settings.render
    if render.width <= 0 or render.height <= 0:
        raise ConfigError("render.width/height must be positive")
    if render.width >= render.height:
        raise ConfigError(
            "render dimensions must be vertical (9:16); got "
            f"{render.width}x{render.height}"
        )
    if render.fps not in (24, 25, 30, 50, 60):
        raise ConfigError(f"render.fps {render.fps} is unusual for TikTok; use 24/25/30/50/60")
    if not 0 <= render.crf <= 51:
        raise ConfigError("render.crf must be between 0 and 51")
    if settings.schedule.videos_per_day < 1:
        raise ConfigError("schedule.videos_per_day must be >= 1")
    if settings.concurrency < 1:
        raise ConfigError("concurrency must be >= 1")


#: Guard against `a extends b extends a`.
_MAX_CHANNEL_DEPTH = 6


def _resolve_channel_data(
    settings: Settings, name: str, seen: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Read a channel file, applying ``extends`` inheritance.

    A channel may declare ``extends: <other_channel>`` to inherit that
    channel's entire configuration, then override parts of it. This is what
    makes a second faceless account (cats only, farm animals, fantasy
    creatures) a ~40-line file instead of a 700-line copy.

    Merge rules:

    * scalars and mappings deep-merge, child wins;
    * a vocabulary pool defined in the child **replaces** the parent's pool
      entirely (partial list merging is ambiguous and surprising);
    * ``vocabulary_remove: {pool: [id, ...]}`` prunes inherited entries, which
      is how you say "same as the parent, minus the dogs".
    """
    if name in seen:
        raise ConfigError(f"Circular channel inheritance: {' -> '.join([*seen, name])}")
    if len(seen) >= _MAX_CHANNEL_DEPTH:
        raise ConfigError(f"Channel inheritance nested deeper than {_MAX_CHANNEL_DEPTH} levels")

    data = _read_yaml(settings.channel_path(name))
    parent_name = data.pop("extends", None)
    if not parent_name:
        return data

    parent = _resolve_channel_data(settings, str(parent_name), (*seen, name))
    removals = data.pop("vocabulary_remove", {}) or {}
    merged = _deep_merge(parent, data)

    for pool, ids in removals.items():
        entries = merged.get("vocabulary", {}).get(pool)
        if entries is None:
            raise ConfigError(
                f"Channel {name!r} tries to remove from unknown pool {pool!r}"
            )
        drop = set(ids or [])
        merged["vocabulary"][pool] = [e for e in entries if e.get("id") not in drop]
        if not merged["vocabulary"][pool]:
            raise ConfigError(
                f"Channel {name!r} removed every option from pool {pool!r}; "
                f"leave at least one."
            )
    return merged


def load_channel(settings: Settings, name: str | None = None) -> ChannelProfile:
    """Load a channel profile by name (defaults to ``settings.default_channel``)."""
    channel_name = name or settings.default_channel
    path = settings.channel_path(channel_name)
    data = _resolve_channel_data(settings, channel_name)

    missing = [k for k in ("vocabulary", "structure", "copy") if k not in data]
    if missing:
        raise ConfigError(f"Channel {channel_name!r} is missing section(s): {', '.join(missing)}")

    return ChannelProfile(
        name=channel_name,
        display_name=data.get("display_name", channel_name.replace("_", " ").title()),
        description=data.get("description", ""),
        vocabulary=data["vocabulary"],
        structure=data["structure"],
        copy=data["copy"],
        hashtags=data.get("hashtags", {}),
        prompt_style=data.get("prompt_style", {}),
        safety=data.get("safety", {}),
        path=path,
    )


def list_channels(settings: Settings) -> list[str]:
    return sorted(p.stem for p in settings.channels_dir.glob("*.yaml"))
