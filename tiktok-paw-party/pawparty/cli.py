"""Command-line interface.

    pawparty doctor                     check the environment
    pawparty init                       create the workspace folders
    pawparty channels                   list channels + creative space size
    pawparty wire <model>               propose provider config from a model schema
    pawparty ideas --count 7            concepts only, no rendering, no cost
    pawparty run --count 7              the daily batch: generate + render
    pawparty build <concept-id>         resume or re-render one video
    pawparty schedule                   plan today's posting slots
    pawparty publish --manual           export a post-ready upload bundle
    pawparty costs                      spend report
    pawparty stats                      novelty / library stats
    pawparty clean --keep-days 14       drop old intermediates

Every command takes ``--config``, ``--channel``, ``--verbose`` and ``--quiet``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from . import __version__
from .budget import BudgetGuard
from .config import ConfigError, Settings, list_channels, load_channel, load_settings
from .ideas.concept import ConceptGenerator
from .ideas.novelty import NoveltyLedger
from .logging_setup import get_logger, setup_logging
from .media import ffmpeg
from .models import Stage
from .pipeline import Pipeline
from .publishing import manual_export
from .publishing.tiktok_api import TikTokPublisher
from .scheduling.scheduler import Scheduler
from .storage import Workspace
from .util import today_stamp, write_json

log = get_logger("cli")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pawparty",
        description="Automated faceless short-form video pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"pawparty {__version__}")
    parser.add_argument("--config", type=Path, help="path to settings.yaml")
    parser.add_argument("--channel", help="channel name (default: settings.default_channel)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check ffmpeg, config, credentials and disk")
    sub.add_parser("init", help="create the workspace folder tree")
    sub.add_parser("channels", help="list channels and their creative space")

    ideas = sub.add_parser("ideas", help="generate concepts only (free, no rendering)")
    ideas.add_argument("-n", "--count", type=int, default=7)
    ideas.add_argument("--seed", type=int, help="reproduce an exact batch")
    ideas.add_argument("--occasion", help="force a seasonal occasion, e.g. halloween")
    ideas.add_argument("--save", action="store_true", help="write concepts to Prompts/")
    ideas.add_argument("--json", action="store_true", help="print raw JSON")

    run = sub.add_parser("run", help="generate and render a batch of videos")
    run.add_argument("-n", "--count", type=int, help="default: schedule.videos_per_day")
    run.add_argument("--seed", type=int)
    run.add_argument("--date", help="YYYY-MM-DD (default: today)")
    run.add_argument("--occasion")
    run.add_argument("-j", "--concurrency", type=int)
    run.add_argument(
        "--stop-after",
        choices=[s.value for s in Stage],
        help="stop each job after this stage (e.g. clips) — useful for dry runs",
    )
    run.add_argument("--schedule", action="store_true", help="also plan posting slots")
    run.add_argument("--export", action="store_true", help="also write the upload bundle")

    build = sub.add_parser("build", help="resume or rebuild one video by concept id")
    build.add_argument("concept_id")
    build.add_argument("--date", help="YYYY-MM-DD the concept belongs to")
    build.add_argument("--from-stage", choices=[s.value for s in Stage],
                       help="clear this stage and everything after it, then rebuild")

    schedule = sub.add_parser("schedule", help="plan posting slots for a day")
    schedule.add_argument("--date")
    schedule.add_argument("--export", action="store_true", help="also write the upload bundle")

    publish = sub.add_parser("publish", help="export for manual posting, or post via API")
    publish.add_argument("--date")
    publish.add_argument("--manual", action="store_true",
                         help="write an upload bundle instead of calling the API")
    publish.add_argument("--mode", choices=["inbox", "direct"], default="inbox")
    publish.add_argument("--privacy", default="SELF_ONLY",
                         choices=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
    publish.add_argument("--due-only", action="store_true",
                         help="only post slots whose scheduled time has arrived")
    publish.add_argument("--dry-run", action="store_true")

    wire = sub.add_parser(
        "wire",
        help="read a model's input schema and propose the provider config",
    )
    wire.add_argument("model", help="model slug, e.g. owner/name or owner/name:version")
    wire.add_argument("--provider", choices=["replicate"], default="replicate")
    wire.add_argument(
        "--kind", choices=["video", "image"], default="video",
        help="which provider slot this model fills",
    )
    wire.add_argument(
        "--apply", action="store_true",
        help="write the block to config/settings.local.yaml (git-ignored)",
    )
    wire.add_argument("--show-schema", action="store_true", help="dump every input the model takes")

    costs = sub.add_parser("costs", help="provider spend report")
    costs.add_argument("--days", type=int, default=14)

    stats = sub.add_parser("stats", help="novelty ledger and library statistics")
    stats.add_argument("--top", type=int, default=15)

    clean = sub.add_parser("clean", help="delete old intermediate files")
    clean.add_argument("--keep-days", type=int, default=14)

    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """Answer 'why won't it run?' in one screen."""
    print(f"pawparty {__version__}\n")
    ok = True

    print("environment")
    print(f"  python              {sys.version.split()[0]}")
    if ffmpeg.is_available():
        print(f"  ffmpeg              {ffmpeg.version()}")
    else:
        print("  ffmpeg              MISSING — install ffmpeg (see README)")
        ok = False

    print("\nconfig")
    print(f"  project root        {settings.project_root}")
    print(f"  workspace           {settings.workspace}")
    print(f"  default channel     {settings.default_channel}")
    print(f"  render              {settings.render.width}x{settings.render.height} "
          f"@ {settings.render.fps}fps, crf {settings.render.crf}")
    print(f"  videos per day      {settings.schedule.videos_per_day}")
    print(f"  concurrency         {settings.concurrency}")

    try:
        channel = load_channel(settings, args.channel)
        pools = {k: len(v or []) for k, v in channel.vocabulary.items()}
        print(f"  channel {channel.name!r:<12} {sum(pools.values())} vocabulary options "
              f"across {len(pools)} pools")
    except ConfigError as exc:
        print(f"  channel             ERROR: {exc}")
        ok = False

    local_config = settings.project_root / "config" / "settings.local.yaml"
    if local_config.exists():
        print(f"  local overrides     {local_config.name} (applied)")

    print("\nproviders (configured)")
    print(f"  video               {settings.providers.video}")
    print(f"  image               {settings.providers.image}")
    print(f"  music               {settings.providers.music}")
    print(f"  llm                 {settings.providers.llm}")

    # `pawparty wire` leaves TODO placeholders for required inputs it couldn't
    # map. Left in, they fail at generation time — after a batch has started
    # spending. Catch them here instead.
    for provider_name, options in (settings.providers.options or {}).items():
        for section in ("static_input", "input_map"):
            for key, value in (options.get(section) or {}).items():
                if str(value).strip().upper() == "TODO":
                    print(f"  ! {provider_name}.{section}.{key} is still 'TODO' — "
                          f"fill it in before running")
                    ok = False

    # Sanity-check the budget against what a full day would actually cost. A
    # cap below the batch cost isn't a safety net, it's a batch that stops
    # half way through with a confusing error.
    per_second = 0.0
    for key in ("replicate", "fal"):
        opts = (settings.providers.options or {}).get(key) or {}
        if settings.providers.video == key:
            per_second = float(opts.get("cost_per_second_usd", 0.0))
    if per_second > 0:
        # ~31 clips/day at 7 videos/day, and models bill in 5s blocks.
        clips_per_day = int(settings.schedule.videos_per_day * 4.4)
        estimated = clips_per_day * 5 * per_second
        print(f"\nbudget")
        print(f"  daily cap           ${settings.budget.daily_usd:.2f}")
        print(f"  estimated daily     ${estimated:.2f}  "
              f"(~{clips_per_day} clips x 5s x ${per_second}/s)")
        if estimated > settings.budget.daily_usd:
            print(f"  ! the cap is below a full day's cost — the batch will stop early.")
            print(f"    Raise budget.daily_usd to ~${estimated * 1.2:.0f}, or lower")
            print(f"    schedule.videos_per_day to {max(1, int(settings.budget.daily_usd / (estimated / settings.schedule.videos_per_day)))}.")
            ok = False

    print("\ncredentials")
    import os

    for name, why in [
        ("REPLICATE_API_TOKEN", "Replicate video/image"),
        ("FAL_KEY", "fal.ai video"),
        ("ANTHROPIC_API_KEY", "LLM copywriting"),
        ("TIKTOK_ACCESS_TOKEN", "automated posting"),
    ]:
        present = "set" if os.getenv(name) else "not set"
        print(f"  {name:<22}{present:<10}({why})")

    print("\nworkspace")
    try:
        workspace = Workspace.create(settings.workspace)
        free = workspace.disk_free_mb()
        print(f"  folders             ok ({workspace.root})")
        print(f"  free disk           {free / 1024:.1f} GB"
              f"{'  ← LOW' if free < 2000 else ''}")
        finished = len(list(workspace.finished.glob("*/*/final.mp4")))
        print(f"  finished videos     {finished}")
        ledger = NoveltyLedger(workspace.novelty_db)
        print(f"  concepts in ledger  {ledger.count()}")
    except Exception as exc:
        print(f"  workspace           ERROR: {exc}")
        ok = False

    if settings.providers.video == "synth":
        print(
            "\nnote: providers.video is 'synth' — runs are free previews, not\n"
            "      publishable footage. See docs/PROVIDERS.md to switch."
        )

    print("\n" + ("all checks passed" if ok else "problems found — see above"))
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace, settings: Settings) -> int:
    workspace = Workspace.create(settings.workspace)
    print(f"workspace ready at {workspace.root}")
    for name in sorted(p.name for p in workspace.root.iterdir() if p.is_dir()):
        print(f"  {name}/")
    print(
        "\nNext:\n"
        "  1. cp .env.example .env      (optional — only needed for paid providers)\n"
        "  2. pawparty doctor\n"
        "  3. pawparty run --count 2    (free preview run)"
    )
    return 0


def cmd_channels(args: argparse.Namespace, settings: Settings) -> int:
    names = list_channels(settings)
    if not names:
        print("no channels found in config/channels/")
        return 1
    for name in names:
        channel = load_channel(settings, name)
        generator = ConceptGenerator(settings, channel)
        estimate = generator.combination_estimate()
        marker = " (default)" if name == settings.default_channel else ""
        print(f"\n{name}{marker}")
        print(f"  {channel.display_name} — {channel.description}")
        print(f"  vocabulary pools : {len(channel.vocabulary)}")
        for pool, size in sorted(estimate["pool_sizes"].items()):
            print(f"    {pool:<18}{size}")
        print(f"  core combinations: {estimate['core_combinations']:,}")
        print(f"  with cast/props  : ~{estimate['with_cast_and_accessories']:,}")
    return 0


def cmd_ideas(args: argparse.Namespace, settings: Settings) -> int:
    channel = load_channel(settings, args.channel)
    workspace = Workspace.create(settings.workspace)
    ledger = NoveltyLedger(workspace.novelty_db)
    generator = ConceptGenerator(settings, channel, ledger)

    concepts = generator.generate_batch(
        args.count, seed=args.seed, force_occasion=args.occasion
    )

    if args.json:
        print(json.dumps([c.to_dict() for c in concepts], indent=2))
    else:
        for index, concept in enumerate(concepts, start=1):
            print(f"\n{index}. {concept.title}")
            print(f"   id        {concept.id}   seed {concept.seed}")
            print(f"   cast      {concept.cast_summary}")
            print(f"   setting   {concept.setting_label}  |  palette {concept.palette_label}")
            print(f"   music     {concept.music.label} ({concept.music.bpm} BPM, "
                  f"{concept.music.energy})")
            print(f"   effects   {', '.join(concept.effects) or '—'}")
            print(f"   length    {concept.duration_s:.0f}s across {len(concept.beats)} beats "
                  f"(loop: {concept.loop_strategy})")
            for beat in concept.beats:
                print(f"     {beat.index}. [{beat.role:<11}] {beat.duration_s:>4.1f}s  "
                      f"{beat.camera:<16} {beat.transition_in}")

    if args.save:
        from .prompts.renderer import PromptRenderer

        renderer = PromptRenderer(settings, channel)
        out_dir = workspace.day(workspace.prompts)
        for concept in concepts:
            renderer.render_all(concept)
            generator.commit(concept)
            write_json(out_dir / f"{concept.id}.json", concept.to_dict())
        print(f"\nsaved {len(concepts)} concept(s) to {out_dir}")
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if not ffmpeg.is_available():
        print("ffmpeg is required to render. Run `pawparty doctor` for install hints.")
        return 1

    channel = load_channel(settings, args.channel)
    pipeline = Pipeline(settings, channel)
    if not pipeline.free_space_ok():
        return 1

    date = _parse_date(args.date)
    result = pipeline.run_batch(
        count=args.count,
        date=date,
        seed=args.seed,
        force_occasion=args.occasion,
        concurrency=args.concurrency,
        stop_after=Stage(args.stop_after) if args.stop_after else None,
    )

    print(f"\n{result.summary()}")
    for job in result.succeeded:
        print(f"  ok    {job.concept.title}")
        print(f"        {job.final_path}")
        if job.copy:
            print(f"        \"{job.copy.chosen_caption}\"")
    for job in result.failed:
        print(f"  FAIL  {job.concept.title}\n        {job.error}")
    if result.degraded_providers:
        print(f"\n  degraded providers: {', '.join(result.degraded_providers)}")

    if args.schedule and result.succeeded:
        scheduler = Scheduler(settings.schedule, pipeline.workspace)
        slots = scheduler.plan(result.succeeded, date=date)
        print(f"\nscheduled {len(slots)} post(s):")
        for slot in slots:
            print(f"  {slot.post_at_local[11:16]}  {slot.title}")
        if args.export:
            folder = manual_export(slots, pipeline.workspace, date=date)
            print(f"\nupload bundle: {folder}")

    return 0 if not result.failed else 2


def cmd_build(args: argparse.Namespace, settings: Settings) -> int:
    channel = load_channel(settings, args.channel)
    pipeline = Pipeline(settings, channel)
    stamp = args.date or today_stamp()

    try:
        job = pipeline.load_job(args.concept_id, stamp)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    if args.from_stage:
        order = Stage.order()
        start = order.index(Stage(args.from_stage))
        for stage in order[start:]:
            job.stages.pop(stage.value, None)
        print(f"cleared stages from {args.from_stage} onward")

    job = pipeline.build(job)
    print(f"{job.status}: {job.final_path or job.error}")
    return 0 if job.status == "done" else 2


def cmd_schedule(args: argparse.Namespace, settings: Settings) -> int:
    channel = load_channel(settings, args.channel)
    pipeline = Pipeline(settings, channel)
    date = _parse_date(args.date) or _dt.date.today()
    stamp = today_stamp(date)

    jobs = []
    day_dir = pipeline.workspace.generated / stamp
    if day_dir.exists():
        for manifest in sorted(day_dir.glob("*/manifest.json")):
            try:
                job = pipeline.load_job(manifest.parent.name, stamp)
                if job.status == "done":
                    jobs.append(job)
            except Exception as exc:
                log.warning("could not load %s: %s", manifest, exc)

    if not jobs:
        print(f"no completed videos found for {stamp}")
        return 1

    scheduler = Scheduler(settings.schedule, pipeline.workspace)
    slots = scheduler.plan(jobs, date=date)
    print(f"planned {len(slots)} post(s) for {stamp} ({settings.schedule.timezone}):")
    for slot in slots:
        print(f"  {slot.post_at_local[11:16]}  {slot.title}")
        print(f"            \"{slot.caption}\"")

    if args.export:
        folder = manual_export(slots, pipeline.workspace, date=date)
        print(f"\nupload bundle: {folder}")
    return 0


def cmd_publish(args: argparse.Namespace, settings: Settings) -> int:
    workspace = Workspace.create(settings.workspace)
    scheduler = Scheduler(settings.schedule, workspace)
    date = _parse_date(args.date) or _dt.date.today()

    slots = scheduler.due(date=date) if args.due_only else scheduler.load(date)
    slots = [s for s in slots if s.status == "scheduled"]
    if not slots:
        print(f"nothing to publish for {today_stamp(date)}")
        return 0

    if args.manual:
        folder = manual_export(slots, workspace, date=date)
        print(f"upload bundle for {len(slots)} video(s): {folder}")
        return 0

    publisher = TikTokPublisher(mode=args.mode)
    if not publisher.available():
        print(
            "TIKTOK_ACCESS_TOKEN is not set, so automated posting is unavailable.\n"
            "Automated posting requires an approved TikTok developer app — see\n"
            "docs/PUBLISHING.md. Falling back to a manual upload bundle."
        )
        folder = manual_export(slots, workspace, date=date)
        print(f"upload bundle: {folder}")
        return 0

    failures = 0
    for slot in slots:
        caption = f"{slot.caption} " + " ".join(f"#{t}" for t in slot.hashtags)
        if args.dry_run:
            print(f"[dry-run] would post {slot.video_path}\n          {caption}")
            continue
        try:
            result = publisher.publish(
                Path(slot.video_path),
                caption=caption.strip(),
                privacy_level=args.privacy,
                is_ai_generated=True,
            )
            slot.status = "published"
            slot.published_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            slot.metadata["publish_id"] = result.publish_id
            slot.metadata["publish_status"] = result.status
            print(f"posted {slot.title} -> {result.status} ({result.publish_id})")
        except Exception as exc:
            failures += 1
            slot.status = "failed"
            slot.error = str(exc)
            print(f"FAILED {slot.title}: {exc}")
        scheduler.update_slot(slot, date=date)

    return 0 if not failures else 2


def cmd_wire(args: argparse.Namespace, settings: Settings) -> int:
    """Fetch a model's schema and propose the config to run it.

    Advisory by design: it prints what it worked out and what it couldn't,
    rather than silently guessing. A wrong `input_map` fails at generation
    time — after a batch has already started spending.
    """
    from .providers.base import ProviderError, ProviderUnavailable
    from .providers.schema import fetch_replicate_schema, propose_wiring

    try:
        schema = fetch_replicate_schema(args.model)
    except (ProviderUnavailable, ProviderError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(f"\n{schema.slug}  ({schema.provider})")
    if schema.description:
        print(f"  {schema.description[:150]}")
    print(f"  inputs: {', '.join(schema.names())}\n")

    if args.show_schema:
        for name in schema.names():
            prop = schema.properties[name]
            bits = [prop.get("type", "?")]
            if prop.get("enum"):
                bits.append(f"one of {prop['enum']}")
            if "default" in prop:
                bits.append(f"default={prop['default']!r}")
            if name in schema.required:
                bits.append("REQUIRED")
            print(f"  {name:<22}{'  |  '.join(str(b) for b in bits)}")
        print()

    proposal = propose_wiring(schema)
    provider_key = "replicate" if args.kind == "video" else "replicate_image"

    print("proposed config")
    print("-" * 60)
    print(f"providers:\n  {args.kind}: {args.provider}\n  options:")
    print(proposal.to_yaml(provider_key))
    print("-" * 60)

    if proposal.is_image_to_video:
        print(
            "\nThis model takes a start image, so PawParty will generate a still per\n"
            "beat and animate it. That keeps the same kitten looking like the same\n"
            "kitten across beats — worth the extra cent. Set providers.image too."
        )
    else:
        print(
            "\nText-to-video: each beat is generated independently, so the animal's\n"
            "markings will drift between shots. Acceptable for short videos; use an\n"
            "image-to-video model if that bothers you."
        )

    for note in proposal.notes:
        print(f"  note: {note}")
    for warning in proposal.warnings:
        print(f"  WARNING: {warning}")

    if args.apply:
        local = settings.project_root / "config" / "settings.local.yaml"
        existing = {}
        if local.exists():
            import yaml as _yaml

            existing = _yaml.safe_load(local.read_text(encoding="utf-8")) or {}

        providers = existing.setdefault("providers", {})
        providers[args.kind] = args.provider
        options = providers.setdefault("options", {})
        options[provider_key] = {
            "model": schema.slug,
            "cost_per_second_usd": 0.05,
            "timeout_s": 900,
            "input_map": proposal.input_map,
            **({"static_input": proposal.static_input} if proposal.static_input else {}),
        }
        if args.kind == "image":
            entry = options[provider_key]
            entry.pop("cost_per_second_usd", None)
            entry["cost_per_image_usd"] = 0.01

        import yaml as _yaml

        local.write_text(
            "# Machine-local overrides. Git-ignored. Deep-merged over settings.yaml.\n"
            "# Written by `pawparty wire` — edit freely.\n\n"
            + _yaml.safe_dump(existing, sort_keys=False),
            encoding="utf-8",
        )
        print(f"\nwrote {local}")
        print("  Check cost_per_second_usd against the model's pricing page — the")
        print("  budget guard is only as accurate as that number.")
        print("\nNext:  pawparty doctor  &&  pawparty run --count 1 -v")
    else:
        print("\nRe-run with --apply to write this to config/settings.local.yaml.")

    return 0 if not proposal.warnings else 0


def cmd_costs(args: argparse.Namespace, settings: Settings) -> int:
    workspace = Workspace.create(settings.workspace)
    guard = BudgetGuard(settings.budget, workspace.cost_ledger)
    rows = guard.report(args.days)
    if not rows:
        print("no spend recorded yet")
        return 0
    print(f"{'date':<12}{'spend':>10}")
    total = 0.0
    for stamp, amount in rows:
        total += amount
        print(f"{stamp:<12}{amount:>10.3f}")
    print(f"{'total':<12}{total:>10.3f}")
    print(f"\ncaps: ${settings.budget.daily_usd:.2f}/day, "
          f"${settings.budget.per_video_usd:.2f}/video")
    print(f"remaining today: ${guard.remaining_today():.2f}")
    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    channel = load_channel(settings, args.channel)
    workspace = Workspace.create(settings.workspace)
    ledger = NoveltyLedger(workspace.novelty_db)

    print(f"channel: {channel.name}")
    print(f"  concepts recorded : {ledger.count(channel.name)}")
    print(f"  all channels      : {ledger.count()}")

    finished = sorted(workspace.finished.glob("*/*/final.mp4"))
    print(f"  finished videos   : {len(finished)}")

    leaders = ledger.option_leaderboard(channel.name, args.top)
    if leaders:
        print(f"\nmost-used options (top {args.top}) — a run-away entry here means "
              f"the vocabulary needs more variety:")
        for option_id, count in leaders:
            print(f"  {count:>4}x  {option_id}")
    return 0


def cmd_clean(args: argparse.Namespace, settings: Settings) -> int:
    channel = load_channel(settings, args.channel)
    pipeline = Pipeline(settings, channel)
    removed = pipeline.cleanup_old_days(args.keep_days)
    print(f"removed {removed} old intermediate folder(s); "
          f"Finished/ was not touched")
    print(f"free disk: {pipeline.workspace.disk_free_mb() / 1024:.1f} GB")
    return 0


# --------------------------------------------------------------------------- #
COMMANDS = {
    "doctor": cmd_doctor,
    "init": cmd_init,
    "channels": cmd_channels,
    "ideas": cmd_ideas,
    "run": cmd_run,
    "build": cmd_build,
    "schedule": cmd_schedule,
    "publish": cmd_publish,
    "wire": cmd_wire,
    "costs": cmd_costs,
    "stats": cmd_stats,
    "clean": cmd_clean,
}


def _parse_date(value: str | None) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--date must be YYYY-MM-DD, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    log_file = None
    if args.command not in ("doctor", "channels"):
        try:
            log_file = Workspace.create(settings.workspace).run_log()
        except OSError as exc:
            print(f"could not open the workspace: {exc}", file=sys.stderr)
            return 1
    setup_logging(log_file, verbose=args.verbose, quiet=args.quiet)

    try:
        return COMMANDS[args.command](args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
