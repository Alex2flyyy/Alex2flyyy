"""The orchestrator.

One job = one video. :meth:`Pipeline.build` walks a job through the stages in
:class:`~pawparty.models.Stage` order, persisting ``manifest.json`` after each
one. Because state lives on disk rather than in memory:

* a crashed run resumes exactly where it stopped (``pawparty build <id>``),
* a single bad beat can be re-rendered without redoing the rest,
* and every finished video carries a complete record of how it was made.

:meth:`Pipeline.run_batch` is the daily entry point: generate N concepts, build
them (optionally in parallel), and report. Failures are isolated — one broken
job never takes down the night.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .budget import BudgetExceeded, BudgetGuard
from .config import ChannelProfile, Settings, load_channel
from .copywriting.captions import CaptionWriter
from .copywriting.hashtags import HashtagBuilder
from .copywriting.onscreen import build_subtitle_lines
from .ideas.concept import ConceptGenerator
from .ideas.novelty import NoveltyLedger
from .logging_setup import get_logger
from .media import assemble, ffmpeg, qc, subtitles
from .models import Beat, Concept, CopyPack, JobRecord, JobStatus, Stage
from .prompts.renderer import PromptRenderer
from .providers.base import ProviderError
from .providers.registry import ProviderBundle, build_providers
from .storage import Workspace
from .util import iso, read_json, retry, today_stamp, write_json, write_text

log = get_logger("pipeline")


@dataclass
class BatchResult:
    date: str
    channel: str
    succeeded: list[JobRecord] = field(default_factory=list)
    failed: list[JobRecord] = field(default_factory=list)
    total_cost_usd: float = 0.0
    degraded_providers: list[str] = field(default_factory=list)

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.succeeded), len(self.failed)

    def summary(self) -> str:
        ok, bad = self.counts
        return (
            f"{ok} succeeded, {bad} failed on {self.date} "
            f"({self.channel}) — ${self.total_cost_usd:.2f} spent"
        )


class Pipeline:
    """Builds videos for one channel."""

    def __init__(
        self,
        settings: Settings,
        channel: ChannelProfile | None = None,
        *,
        workspace: Workspace | None = None,
        providers: ProviderBundle | None = None,
    ) -> None:
        self.settings = settings
        self.channel = channel or load_channel(settings)
        self.workspace = workspace or Workspace.create(settings.workspace)
        self.providers = providers or build_providers(settings, self.workspace)
        self.ledger = NoveltyLedger(self.workspace.novelty_db)
        self.generator = ConceptGenerator(settings, self.channel, self.ledger)
        self.renderer = PromptRenderer(settings, self.channel)
        self.captions = CaptionWriter(self.channel, self.providers.llm)
        self.hashtags = HashtagBuilder(self.channel)
        self.budget = BudgetGuard(settings.budget, self.workspace.cost_ledger)
        #: Caption openings already used in this run. Jobs render in parallel,
        #: so this is guarded — two threads reading an unguarded set at the same
        #: moment would both think their caption was unused and pick the same one.
        self._used_caption_shapes: set[str] = set()
        self._used_hooks: set[str] = set()
        self._caption_lock = threading.Lock()

    # ================================================================== #
    # Batch
    # ================================================================== #
    def run_batch(
        self,
        count: int | None = None,
        *,
        date: _dt.date | None = None,
        seed: int | None = None,
        force_occasion: str | None = None,
        concurrency: int | None = None,
        stop_after: Stage | None = None,
    ) -> BatchResult:
        """Generate and build a day's worth of videos."""
        count = count or self.settings.schedule.videos_per_day
        date = date or _dt.date.today()
        stamp = today_stamp(date)
        workers = max(1, concurrency or self.settings.concurrency)

        log.info(
            "starting batch: %d video(s) for %s on channel %r (concurrency %d)",
            count, stamp, self.channel.name, workers,
        )
        if self.providers.is_preview_only:
            log.warning(
                "video provider is 'synth' — this batch produces PREVIEWS, not "
                "publishable footage. Set providers.video in config/settings.yaml."
            )

        concepts = self.generator.generate_batch(
            count, seed=seed, date=date, force_occasion=force_occasion
        )
        # Concepts are already committed to the novelty ledger by the generator,
        # one at a time, so each draw steers the next.
        for concept in concepts:
            log.info("concept: %s (%.0fs, %d beats)",
                     concept.title, concept.duration_s, len(concept.beats),
                     extra={"concept": concept.id})

        result = BatchResult(
            date=stamp,
            channel=self.channel.name,
            degraded_providers=list(self.providers.degraded),
        )

        if workers == 1:
            jobs = [self._safe_build(c, date, stop_after) for c in concepts]
        else:
            jobs = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._safe_build, c, date, stop_after): c for c in concepts
                }
                for future in as_completed(futures):
                    jobs.append(future.result())

        for job in jobs:
            result.total_cost_usd += job.cost_usd
            if job.status == JobStatus.DONE.value:
                result.succeeded.append(job)
            else:
                result.failed.append(job)

        result.total_cost_usd = round(result.total_cost_usd, 4)
        self._write_batch_report(result, date)
        log.info("batch complete: %s", result.summary())
        return result

    def _safe_build(
        self, concept: Concept, date: _dt.date, stop_after: Stage | None
    ) -> JobRecord:
        """Build one video, converting any exception into a failed JobRecord."""
        job = JobRecord(concept=concept, date=today_stamp(date))
        try:
            return self.build(job, stop_after=stop_after)
        except BudgetExceeded as exc:
            job.status = JobStatus.FAILED.value
            job.error = f"budget: {exc}"
            log.error("budget stop: %s", exc, extra={"concept": concept.id})
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            job.status = JobStatus.FAILED.value
            job.error = f"{type(exc).__name__}: {exc}"
            log.error(
                "job failed: %s\n%s", exc, traceback.format_exc(),
                extra={"concept": concept.id},
            )
        self._save(job)
        return job

    # ================================================================== #
    # Single job
    # ================================================================== #
    def build(self, job: JobRecord, *, stop_after: Stage | None = None) -> JobRecord:
        """Run every stage for one job. Idempotent — completed stages are skipped."""
        concept = job.concept
        job.status = JobStatus.RUNNING.value
        log.info("building %r", concept.title, extra={"concept": concept.id})

        stages = [
            (Stage.PROMPTS, self._stage_prompts),
            (Stage.STILLS, self._stage_stills),
            (Stage.CLIPS, self._stage_clips),
            (Stage.MOTION, self._stage_motion),
            (Stage.ASSEMBLE, self._stage_assemble),
            (Stage.MUSIC, self._stage_music),
            (Stage.COPY, self._stage_copy),
            (Stage.SUBTITLES, self._stage_subtitles),
            (Stage.PACKAGE, self._stage_package),
            (Stage.QC, self._stage_qc),
        ]

        for stage, handler in stages:
            if job.is_stage_done(stage):
                log.debug("skipping completed stage %s", stage.value,
                          extra={"concept": concept.id})
                continue
            job.begin_stage(stage)
            self._save(job)
            handler(job)
            self._save(job)
            if stop_after and stage == stop_after:
                log.info("stopping after stage %s as requested", stage.value,
                         extra={"concept": concept.id})
                job.status = JobStatus.SKIPPED.value
                self._save(job)
                return job

        job.status = JobStatus.DONE.value
        if not self.settings.keep_intermediates:
            self.workspace.purge_intermediates(concept.id, job.date)
        self._save(job)
        log.info(
            "finished: %s (%.1fs, $%.3f)",
            job.final_path, concept.duration_s, job.cost_usd,
            extra={"concept": concept.id},
        )
        return job

    # ================================================================== #
    # Stages
    # ================================================================== #
    def _stage_prompts(self, job: JobRecord) -> None:
        """Render every prompt and archive it next to the video's other files."""
        concept = job.concept
        self.renderer.render_all(concept)

        prompt_dir = self.workspace.job_dir(self.workspace.prompts, concept.id, job.date)
        write_json(prompt_dir / "concept.json", concept.to_dict())

        readable = [f"# {concept.title}", f"# {concept.id}", ""]
        for beat in concept.beats:
            readable += [
                f"## Beat {beat.index} — {beat.role} ({beat.duration_s:.1f}s, "
                f"camera: {beat.camera}, transition in: {beat.transition_in})",
                "",
                "### video prompt",
                beat.prompt,
                "",
                "### still prompt",
                beat.still_prompt,
                "",
            ]
        readable += ["## negative prompt", concept.beats[0].negative_prompt if concept.beats else "", ""]
        readable += ["## music prompt", self.renderer.render_music(concept), ""]
        write_text(prompt_dir / "prompts.md", "\n".join(readable))

        job.finish_stage(
            Stage.PROMPTS,
            detail=f"{len(concept.beats)} beat prompts",
            artifacts=[str(prompt_dir / "prompts.md")],
        )

    def _stage_stills(self, job: JobRecord) -> None:
        """Generate first frames — only when the video provider wants them.

        Image-to-video gives much better character consistency across beats than
        text-to-video does, so this is on by default whenever the configured
        video provider maps a ``first_frame`` input.
        """
        concept = job.concept
        if not self._uses_first_frames():
            job.finish_stage(Stage.STILLS, status=JobStatus.SKIPPED,
                             detail="video provider does not take first frames")
            return

        out_dir = self.workspace.job_dir(self.workspace.images, concept.id, job.date)
        paths: list[str] = []
        cost = 0.0

        for beat in concept.beats:
            target = out_dir / f"{beat.slug}.png"
            if target.exists() and target.stat().st_size > 0:
                paths.append(str(target))
                continue

            estimate = float(self.providers.image.estimate_cost())
            self.budget.check(estimate, concept_id=concept.id, date=job.date)
            result = retry(
                lambda b=beat, t=target: self.providers.image.generate_image(
                    prompt=b.still_prompt,
                    negative_prompt=b.negative_prompt,
                    width=self.settings.render.width,
                    height=self.settings.render.height,
                    output=t,
                    seed=concept.seed + b.index,
                ),
                attempts=3,
                exceptions=(ProviderError,),
                on_error=lambda attempt, exc: log.warning(
                    "still %s attempt %d failed: %s", beat.slug, attempt, exc,
                    extra={"concept": concept.id},
                ),
            )
            self.budget.record(result.cost_usd, concept_id=concept.id,
                               stage="stills", provider=result.provider, date=job.date)
            cost += result.cost_usd
            paths.append(str(result.path))

        job.still_paths = paths
        job.finish_stage(Stage.STILLS, detail=f"{len(paths)} stills", artifacts=paths, cost_usd=cost)

    def _stage_clips(self, job: JobRecord) -> None:
        """Generate the raw video for each beat."""
        concept = job.concept
        out_dir = self.workspace.job_dir(self.workspace.generated, concept.id, job.date)
        raw_dir = out_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        paths: list[str] = []
        cost = 0.0
        stills = {Path(p).stem: Path(p) for p in job.still_paths}

        for beat in concept.beats:
            target = raw_dir / f"{beat.slug}.mp4"
            if target.exists() and target.stat().st_size > 0:
                log.debug("reusing existing clip %s", target.name, extra={"concept": concept.id})
                paths.append(str(target))
                continue

            estimate = float(self.providers.video.estimate_cost(beat.duration_s))
            self.budget.check(estimate, concept_id=concept.id, date=job.date)

            first_frame = stills.get(beat.slug)
            result = retry(
                lambda b=beat, t=target, f=first_frame: self.providers.video.generate_clip(
                    prompt=b.prompt,
                    negative_prompt=b.negative_prompt,
                    duration_s=b.duration_s,
                    width=self.settings.render.width,
                    height=self.settings.render.height,
                    fps=self.settings.render.fps,
                    output=t,
                    seed=concept.seed + b.index * 7,
                    first_frame=f,
                ),
                attempts=3,
                exceptions=(ProviderError,),
                on_error=lambda attempt, exc: log.warning(
                    "clip %s attempt %d failed: %s", beat.slug, attempt, exc,
                    extra={"concept": concept.id},
                ),
            )
            self.budget.record(result.cost_usd, concept_id=concept.id,
                               stage="clips", provider=result.provider, date=job.date)
            cost += result.cost_usd
            paths.append(str(result.path))
            log.debug("clip %s ready (%.1fs)", beat.slug, beat.duration_s,
                      extra={"concept": concept.id})

        job.clip_paths = paths
        job.finish_stage(Stage.CLIPS, detail=f"{len(paths)} clips",
                         artifacts=paths, cost_usd=cost)

    def _stage_motion(self, job: JobRecord) -> None:
        """Normalise to 9:16 and layer on the camera move."""
        concept = job.concept
        out_dir = self.workspace.job_dir(self.workspace.generated, concept.id, job.date)
        prepared_dir = out_dir / "prepared"
        prepared_dir.mkdir(parents=True, exist_ok=True)

        prepared: list[str] = []
        for beat, raw in zip(concept.beats, job.clip_paths):
            target = prepared_dir / f"{beat.slug}.mp4"
            assemble.prepare_beat(
                Path(raw), beat, self.settings.render, target,
                intensity=self._motion_intensity(beat),
            )
            prepared.append(str(target))

        job.clip_paths = prepared  # downstream stages consume the prepared set
        job.finish_stage(Stage.MOTION, detail=f"{len(prepared)} beats normalised",
                         artifacts=prepared)

    def _stage_assemble(self, job: JobRecord) -> None:
        """Stitch beats together and close the loop."""
        concept = job.concept
        out_dir = self.workspace.job_dir(self.workspace.generated, concept.id, job.date)
        stitched = out_dir / "stitched.mp4"
        looped = out_dir / "silent.mp4"

        assemble.stitch(
            [Path(p) for p in job.clip_paths],
            concept.beats,
            self.settings.render,
            stitched,
            out_dir / "work",
        )
        final_silent = assemble.close_loop(
            stitched, concept, self.settings.render, looped, out_dir / "work"
        )
        job.silent_path = str(final_silent)
        job.finish_stage(Stage.ASSEMBLE, detail="stitched + loop closed",
                         artifacts=[job.silent_path])

    def _stage_music(self, job: JobRecord) -> None:
        """Fetch or generate a music bed matched to the finished video length."""
        concept = job.concept
        actual = ffmpeg.duration_of(job.silent_path) if job.silent_path else concept.duration_s
        out_dir = self.workspace.day(self.workspace.audio_beds, job.date)
        target = out_dir / f"{concept.id}.wav"

        if target.exists() and target.stat().st_size > 0:
            job.music_path = str(target)
            job.finish_stage(Stage.MUSIC, detail="reused existing bed",
                             artifacts=[job.music_path])
            return

        estimate = float(self.providers.music.estimate_cost(actual))
        self.budget.check(estimate, concept_id=concept.id, date=job.date)

        result = retry(
            lambda: self.providers.music.get_track(
                prompt=self.renderer.render_music(concept),
                vibe=concept.music.vibe,
                tags=concept.music.tags,
                bpm=concept.music.bpm,
                # A little extra so loudnorm and the fade have material to work
                # with; `-shortest` trims the tail at mux time.
                duration_s=actual + 0.6,
                output=target,
                seed=concept.seed,
            ),
            attempts=3,
            exceptions=(ProviderError,),
            on_error=lambda attempt, exc: log.warning(
                "music attempt %d failed: %s", attempt, exc, extra={"concept": concept.id},
            ),
        )
        self.budget.record(result.cost_usd, concept_id=concept.id,
                           stage="music", provider=result.provider, date=job.date)
        job.music_path = str(result.path)
        job.finish_stage(Stage.MUSIC, detail=f"{result.provider}: {concept.music.label}",
                         artifacts=[job.music_path], cost_usd=result.cost_usd)

    def _stage_copy(self, job: JobRecord) -> None:
        """Captions, hashtags, on-screen hook, pinned comment."""
        concept = job.concept
        with self._caption_lock:
            taken_shapes = set(self._used_caption_shapes)
            taken_hooks = set(self._used_hooks)
        payload = self.captions.write(
            concept,
            self.renderer.render_copy_prompt(concept),
            avoid_shapes=taken_shapes,
            avoid_hooks=taken_hooks,
        )
        with self._caption_lock:
            self._used_caption_shapes.add(CaptionWriter.shape(payload["chosen_caption"]))
            self._used_hooks.add(payload["onscreen_hook"].upper())

        tags = self.hashtags.build(concept, payload.get("hashtag_ideas", []))

        # Time the cards against the video that actually exists, not the plan —
        # transitions shave a second or two off a long concept.
        actual = ffmpeg.duration_of(job.silent_path) if job.silent_path else None
        lines = build_subtitle_lines(
            concept,
            payload["onscreen_hook"],
            payload.get("comment_seed", ""),
            actual_duration_s=actual,
        )

        job.copy = CopyPack(
            captions=payload["captions"],
            chosen_caption=payload["chosen_caption"],
            hashtags=tags,
            onscreen_hook=payload["onscreen_hook"],
            subtitle_lines=lines,
            alt_text=payload.get("alt_text", ""),
            comment_seed=payload.get("comment_seed", ""),
        )

        caption_dir = self.workspace.job_dir(self.workspace.captions, concept.id, job.date)
        write_json(caption_dir / "copy.json", job.copy.to_dict())
        write_text(
            caption_dir / "captions.txt",
            "\n".join(f"{i + 1}. {c}" for i, c in enumerate(job.copy.captions)),
        )
        write_text(caption_dir / "hashtags.txt", self.hashtags.render(tags))

        cost = 0.0
        if getattr(self.providers.llm, "name", "offline") != "offline":
            cost = float(self.providers.llm.estimate_cost())
            self.budget.record(cost, concept_id=concept.id, stage="copy",
                               provider=self.providers.llm.name, date=job.date)

        job.finish_stage(
            Stage.COPY,
            detail=f"{len(job.copy.captions)} captions, {len(tags)} hashtags",
            artifacts=[str(caption_dir / "copy.json")],
            cost_usd=cost,
        )

    def _stage_subtitles(self, job: JobRecord) -> None:
        """Build the ASS file for burn-in (and an SRT for cross-posting)."""
        concept = job.concept
        cfg = self.settings.subtitles
        lines = job.copy.subtitle_lines if job.copy else []

        if not subtitles.should_burn(cfg, concept.duration_s, bool(lines)):
            job.finish_stage(
                Stage.SUBTITLES,
                status=JobStatus.SKIPPED,
                detail=(
                    "disabled" if not cfg.enabled
                    else f"clip is {concept.duration_s:.0f}s, under the "
                         f"{cfg.min_duration_s:.0f}s burn-in threshold"
                ),
            )
            return

        out_dir = self.workspace.job_dir(self.workspace.captions, concept.id, job.date)
        ass_path = subtitles.build_ass(lines, self.settings.render, cfg, out_dir / "subs.ass")
        subtitles.write_srt(lines, out_dir / "subs.srt")
        job.subtitle_path = str(ass_path)
        job.finish_stage(Stage.SUBTITLES, detail=f"{len(lines)} text cards",
                         artifacts=[job.subtitle_path])

    def _stage_package(self, job: JobRecord) -> None:
        """Final encode into `Finished/`, plus the post-ready sidecars."""
        concept = job.concept
        finished = self.workspace.finished_dir(concept.id, job.date)
        final = finished / "final.mp4"

        assemble.mux(
            Path(job.silent_path),
            Path(job.music_path) if job.music_path else None,
            self.settings,
            final,
            subtitle_file=Path(job.subtitle_path) if job.subtitle_path else None,
            subtitle_style=subtitles.force_style(self.settings.subtitles),
        )
        job.final_path = str(final)

        cover = finished / "cover.jpg"
        try:
            assemble.extract_cover(final, cover)
            job.cover_path = str(cover)
        except Exception as exc:  # a missing cover is not worth failing a video
            log.warning("cover extraction failed: %s", exc, extra={"concept": concept.id})

        if job.copy:
            write_text(finished / "caption.txt", job.copy.chosen_caption)
            write_text(finished / "hashtags.txt", self.hashtags.render(job.copy.hashtags))
            write_text(
                finished / "post.txt",
                f"{job.copy.chosen_caption}\n\n{self.hashtags.render(job.copy.hashtags)}\n",
            )
            write_json(finished / "copy.json", job.copy.to_dict())
        write_json(finished / "concept.json", concept.to_dict())

        job.finish_stage(Stage.PACKAGE, detail=str(final), artifacts=[str(final)])

    def _stage_qc(self, job: JobRecord) -> None:
        """Probe the deliverable and decide whether it is publishable."""
        concept = job.concept
        report = qc.check_video(
            Path(job.final_path),
            concept.duration_s,
            self.settings.qc,
            self.settings.render,
        )

        ok, detail = qc.check_black_frames(Path(job.final_path))
        if not ok:
            report.passed = False
            report.errors.append(f"mostly black video: {detail}")

        job.qc_report = report.to_dict()
        for warning in report.warnings:
            log.warning("QC: %s", warning, extra={"concept": concept.id})

        if not report.passed:
            job.finish_stage(Stage.QC, status=JobStatus.FAILED, detail=report.summary())
            raise RuntimeError(report.summary())

        job.finish_stage(Stage.QC, detail=report.summary())

    # ================================================================== #
    # Helpers
    # ================================================================== #
    def _uses_first_frames(self) -> bool:
        """True when the configured video provider accepts a first-frame image."""
        provider = self.providers.video
        input_map = getattr(provider, "input_map", None)
        if isinstance(input_map, dict) and input_map.get("first_frame"):
            return True
        # The synth provider can animate a still, which is a useful preview of
        # the image-to-video path; enable it only if stills are cheap (synth).
        return (
            getattr(provider, "name", "") == "synth"
            and getattr(self.providers.image, "name", "") == "synth"
        )

    @staticmethod
    def _motion_intensity(beat: Beat) -> float:
        """Hooks get a firmer move; loop beats stay calm so the seam hides."""
        return {"hook": 1.15, "switch": 1.2, "payoff": 1.0, "loop": 0.6}.get(beat.role, 0.9)

    def _save(self, job: JobRecord) -> Path:
        job.updated_at = iso()
        return write_json(
            self.workspace.manifest_path(job.concept.id, job.date), job.to_dict()
        )

    def load_job(self, concept_id: str, date: str | None = None) -> JobRecord:
        """Reload a job from its manifest so a build can be resumed."""
        stamp = date or today_stamp()
        path = self.workspace.manifest_path(concept_id, stamp)
        if not path.exists():
            raise FileNotFoundError(
                f"No manifest for {concept_id} on {stamp}. Looked in {path}."
            )
        return JobRecord.from_dict(read_json(path))

    def _write_batch_report(self, result: BatchResult, date: _dt.date) -> Path:
        report = {
            "date": result.date,
            "channel": result.channel,
            "generated_at": iso(),
            "providers": self.providers.describe(),
            "degraded": result.degraded_providers,
            "total_cost_usd": result.total_cost_usd,
            "succeeded": [
                {
                    "id": j.concept.id,
                    "title": j.concept.title,
                    "duration_s": j.concept.duration_s,
                    "final": j.final_path,
                    "caption": j.copy.chosen_caption if j.copy else "",
                    "cost_usd": j.cost_usd,
                }
                for j in result.succeeded
            ],
            "failed": [
                {"id": j.concept.id, "title": j.concept.title, "error": j.error}
                for j in result.failed
            ],
        }
        return write_json(self.workspace.day(self.workspace.logs, date) / "batch.json", report)

    def free_space_ok(self, need_mb: float = 2000) -> bool:
        free = self.workspace.disk_free_mb()
        if free < need_mb:
            log.error("only %.0fMB free in %s; need ~%.0fMB", free, self.workspace.root, need_mb)
            return False
        return True

    def cleanup_old_days(self, keep_days: int = 14) -> int:
        """Delete `Generated/` partitions older than ``keep_days``.

        `Finished/` is never touched — that's the deliverable.
        """
        cutoff = _dt.date.today() - _dt.timedelta(days=keep_days)
        removed = 0
        for child in sorted(self.workspace.generated.iterdir()):
            if not child.is_dir():
                continue
            try:
                stamp = _dt.date.fromisoformat(child.name)
            except ValueError:
                continue
            if stamp < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
                log.info("removed old intermediates: %s", child.name)
        return removed
