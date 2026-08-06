#!/usr/bin/env bash
#
# Daily batch entry point — the thing cron or systemd calls.
#
#   0 5 * * * /path/to/tiktok-paw-party/scripts/daily_run.sh >> /var/log/pawparty.log 2>&1
#
# Exits non-zero if any video failed, so a monitoring wrapper can notice.
#
# Environment overrides:
#   PAWPARTY_COUNT       videos to make            (default: config value)
#   PAWPARTY_CHANNEL     channel name             (default: config value)
#   PAWPARTY_JOBS        concurrency              (default: config value)
#   PAWPARTY_MIN_FREE_MB abort below this         (default: 3000)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# cron gives you a minimal PATH, which is the most common cron-only failure
# (ffmpeg not found). Be explicit.
export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:${PATH}"

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# --- virtualenv ------------------------------------------------------------- #
if [[ -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

if ! command -v pawparty >/dev/null 2>&1; then
    log "ERROR: pawparty is not on PATH. Did you run 'pip install -e .'?"
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    log "ERROR: ffmpeg is not on PATH. See README install instructions."
    exit 1
fi

# --- disk ------------------------------------------------------------------- #
MIN_FREE_MB="${PAWPARTY_MIN_FREE_MB:-3000}"
WORKSPACE="${PAWPARTY_WORKSPACE:-$PROJECT_ROOT/Videos}"
mkdir -p "$WORKSPACE"

FREE_MB="$(df -Pm "$WORKSPACE" | awk 'NR==2 {print $4}')"
if (( FREE_MB < MIN_FREE_MB )); then
    log "ERROR: only ${FREE_MB}MB free at ${WORKSPACE} (need ${MIN_FREE_MB}MB)."
    log "Try: pawparty clean --keep-days 7"
    exit 1
fi
log "disk ok: ${FREE_MB}MB free"

# --- run -------------------------------------------------------------------- #
ARGS=(run --schedule --export)
[[ -n "${PAWPARTY_COUNT:-}"   ]] && ARGS+=(--count "$PAWPARTY_COUNT")
[[ -n "${PAWPARTY_CHANNEL:-}" ]] && ARGS+=(--channel "$PAWPARTY_CHANNEL")
[[ -n "${PAWPARTY_JOBS:-}"    ]] && ARGS+=(--concurrency "$PAWPARTY_JOBS")

log "starting: pawparty ${ARGS[*]}"
START=$(date +%s)

set +e
pawparty "${ARGS[@]}"
STATUS=$?
set -e

ELAPSED=$(( $(date +%s) - START ))
log "finished in ${ELAPSED}s with exit code ${STATUS}"

# --- summary ---------------------------------------------------------------- #
BATCH_REPORT="${WORKSPACE}/Logs/$(date -u +%F)/batch.json"
if [[ -f "$BATCH_REPORT" ]] && command -v jq >/dev/null 2>&1; then
    log "summary: $(jq -c '{
        ok: (.succeeded | length),
        failed: (.failed | length),
        cost_usd: .total_cost_usd,
        degraded: .degraded
    }' "$BATCH_REPORT")"

    # A degraded provider means the "finished" videos are placeholders. That is
    # worth shouting about — it looks like success otherwise.
    if [[ "$(jq -r '.degraded | length' "$BATCH_REPORT")" != "0" ]]; then
        log "WARNING: a provider degraded to a fallback — check the videos before posting"
    fi
fi

exit "$STATUS"
