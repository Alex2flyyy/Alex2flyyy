"""Core data model.

A *Concept* is the complete creative spec for one video: cast, theme, look,
music, and a list of *Beats* (shots). It is produced entirely before any money
is spent, is JSON-serialisable, and is the single input to rendering. That
separation is what makes the pipeline cheap to iterate on — you can generate a
thousand concepts offline and only render the ones you like.

A *JobRecord* is the runtime wrapper: concept + copy + file paths + stage
status. It is persisted as `manifest.json` after every stage, which is what
makes runs resumable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .util import iso


class Stage(str, Enum):
    """Pipeline stages, in execution order."""

    CONCEPT = "concept"
    PROMPTS = "prompts"
    STILLS = "stills"
    CLIPS = "clips"
    MOTION = "motion"
    ASSEMBLE = "assemble"
    MUSIC = "music"
    SUBTITLES = "subtitles"
    COPY = "copy"
    QC = "qc"
    PACKAGE = "package"

    @classmethod
    def order(cls) -> list["Stage"]:
        return list(cls)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CastMember:
    """One animal performer."""

    species: str            # "kitten" | "puppy" | ...
    breed: str              # "orange tabby", "corgi", ...
    label: str              # human-readable, used in prompts
    personality: str        # drives the funny-expression beat
    accessories: list[str] = field(default_factory=list)
    #: Vocabulary ids behind the labels above. Kept alongside the prose because
    #: the novelty ledger keys on ids — deriving them back from a label is
    #: lossy, and a lossy key means anti-repetition quietly stops working.
    animal_id: str = ""
    accessory_ids: list[str] = field(default_factory=list)

    def describe(self) -> str:
        worn = f" wearing {', '.join(self.accessories)}" if self.accessories else ""
        return f"{self.label}{worn}"


@dataclass
class Beat:
    """One shot. Beats are rendered independently then stitched."""

    index: int
    role: str               # hook | build | switch | payoff | loop
    duration_s: float
    action: str             # what the animals do
    camera: str             # camera move id, e.g. "slow_push_in"
    camera_prompt: str      # natural-language camera description for the model
    transition_in: str      # cut | crossfade | whip_pan | zoom_blur | flash
    prompt: str = ""        # filled by the prompt renderer
    negative_prompt: str = ""
    still_prompt: str = ""  # for image-first providers
    onscreen_text: str = "" # burned-in text for this beat, may be empty

    @property
    def slug(self) -> str:
        return f"beat_{self.index:02d}"


@dataclass
class MusicSpec:
    vibe: str               # id from the channel vocabulary
    label: str
    bpm: int
    energy: str             # low | medium | high
    tags: list[str] = field(default_factory=list)


@dataclass
class Concept:
    """The full creative spec for one video."""

    id: str
    seed: int
    channel: str
    title: str
    theme: str              # theme id, e.g. "disco_night"
    theme_label: str
    cast: list[CastMember]
    dance_style: str
    dance_label: str
    setting: str
    setting_label: str
    palette: str
    palette_label: str
    lighting: str
    effects: list[str]
    effect_ids: list[str]
    music: MusicSpec
    hook: str               # the 0-2s promise, in words
    payoff: str             # the surprise/reward, as a verb phrase for prompts
    #: A noun phrase naming the payoff without spoiling it ("the ending",
    #: "the costume change"). Captions splice this in, and a verb phrase there
    #: reads as broken English ("Wait for the takes an enormous bow").
    payoff_hint: str
    loop_strategy: str      # crossfade_to_start | freeze_hold | hard_cut
    duration_s: float
    beats: list[Beat]
    occasion: str = "none"  # holiday/seasonal tag, "none" for evergreen
    created_at: str = field(default_factory=iso)
    #: Stable fingerprint of the creative choices, used by the novelty ledger.
    signature: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def cast_summary(self) -> str:
        return " and ".join(member.describe() for member in self.cast)

    @property
    def species_mix(self) -> str:
        species = sorted({m.species for m in self.cast})
        return "+".join(species)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Concept":
        data = dict(data)
        data["cast"] = [CastMember(**m) for m in data.get("cast", [])]
        data["beats"] = [Beat(**b) for b in data.get("beats", [])]
        music = data.get("music")
        data["music"] = MusicSpec(**music) if isinstance(music, dict) else music
        return cls(**data)


@dataclass
class CopyPack:
    """Everything text-shaped that ships with the video."""

    captions: list[str]                    # ranked, best first
    chosen_caption: str
    hashtags: list[str]
    onscreen_hook: str                     # burned into the first ~2s
    subtitle_lines: list[dict[str, Any]] = field(default_factory=list)
    alt_text: str = ""
    comment_seed: str = ""                 # first comment to pin, drives replies

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CopyPack":
        return cls(**data)


@dataclass
class StageResult:
    stage: str
    status: str
    started_at: str
    finished_at: str = ""
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class JobRecord:
    """Runtime state for one video. Serialised to `manifest.json`."""

    concept: Concept
    date: str
    status: str = JobStatus.PENDING.value
    copy: CopyPack | None = None
    stages: dict[str, StageResult] = field(default_factory=dict)
    clip_paths: list[str] = field(default_factory=list)
    still_paths: list[str] = field(default_factory=list)
    music_path: str = ""
    subtitle_path: str = ""
    silent_path: str = ""
    final_path: str = ""
    cover_path: str = ""
    qc_report: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str = ""
    updated_at: str = field(default_factory=iso)

    # -- stage bookkeeping -------------------------------------------------- #
    def stage_status(self, stage: Stage) -> str:
        result = self.stages.get(stage.value)
        return result.status if result else JobStatus.PENDING.value

    def is_stage_done(self, stage: Stage) -> bool:
        return self.stage_status(stage) == JobStatus.DONE.value

    def begin_stage(self, stage: Stage) -> StageResult:
        result = StageResult(stage=stage.value, status=JobStatus.RUNNING.value, started_at=iso())
        self.stages[stage.value] = result
        self.updated_at = iso()
        return result

    def finish_stage(
        self,
        stage: Stage,
        *,
        status: JobStatus = JobStatus.DONE,
        detail: str = "",
        artifacts: list[str] | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        result = self.stages.get(stage.value) or self.begin_stage(stage)
        result.status = status.value
        result.finished_at = iso()
        result.detail = detail
        result.artifacts = artifacts or []
        result.cost_usd = cost_usd
        self.cost_usd = round(self.cost_usd + cost_usd, 4)
        self.updated_at = iso()

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept.to_dict(),
            "date": self.date,
            "status": self.status,
            "copy": self.copy.to_dict() if self.copy else None,
            "stages": {k: asdict(v) for k, v in self.stages.items()},
            "clip_paths": self.clip_paths,
            "still_paths": self.still_paths,
            "music_path": self.music_path,
            "subtitle_path": self.subtitle_path,
            "silent_path": self.silent_path,
            "final_path": self.final_path,
            "cover_path": self.cover_path,
            "qc_report": self.qc_report,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        copy_data = data.get("copy")
        return cls(
            concept=Concept.from_dict(data["concept"]),
            date=data["date"],
            status=data.get("status", JobStatus.PENDING.value),
            copy=CopyPack.from_dict(copy_data) if copy_data else None,
            stages={k: StageResult(**v) for k, v in (data.get("stages") or {}).items()},
            clip_paths=data.get("clip_paths", []),
            still_paths=data.get("still_paths", []),
            music_path=data.get("music_path", ""),
            subtitle_path=data.get("subtitle_path", ""),
            silent_path=data.get("silent_path", ""),
            final_path=data.get("final_path", ""),
            cover_path=data.get("cover_path", ""),
            qc_report=data.get("qc_report", {}),
            cost_usd=data.get("cost_usd", 0.0),
            error=data.get("error", ""),
            updated_at=data.get("updated_at", iso()),
        )
