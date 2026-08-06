#!/usr/bin/env bash
#
# One-shot setup: virtualenv, dependencies, workspace, and a health check.
#
#   ./scripts/install.sh          # runtime only
#   ./scripts/install.sh --dev    # plus pytest

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

DEV=0
[[ "${1:-}" == "--dev" ]] && DEV=1

say()  { printf '\n\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mx\033[0m %s\n' "$*"; exit 1; }

# --- python ----------------------------------------------------------------- #
say "Checking Python"
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.10 or newer."

python3 - <<'PY' || die "Python 3.10+ is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
echo "    $(python3 --version)"

# --- ffmpeg ----------------------------------------------------------------- #
say "Checking ffmpeg"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "    $(ffmpeg -version | head -1)"
else
    warn "ffmpeg/ffprobe not found — rendering will not work without it."
    case "$(uname -s)" in
        Darwin) echo "    Install with:  brew install ffmpeg" ;;
        Linux)  echo "    Install with:  sudo apt-get install -y ffmpeg" ;;
        *)      echo "    Install with:  winget install Gyan.FFmpeg" ;;
    esac
fi

# --- virtualenv ------------------------------------------------------------- #
say "Setting up the virtualenv"
[[ -d .venv ]] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip

say "Installing PawParty"
if (( DEV )); then
    pip install --quiet -e . -r requirements-dev.txt
else
    pip install --quiet -e .
fi

# --- config ----------------------------------------------------------------- #
if [[ ! -f .env ]]; then
    say "Creating .env"
    cp .env.example .env
    echo "    Nothing in it is required for the offline preview run."
    echo "    Fill in keys only for the providers you actually use."
fi

say "Creating the workspace"
pawparty init

# --- health ----------------------------------------------------------------- #
say "Health check"
pawparty doctor || warn "doctor reported problems — see above"

cat <<'EOF'

Done. Next:

    source .venv/bin/activate
    pawparty ideas --count 7          # see what it would make (free, instant)
    pawparty run --count 2            # render two preview videos

Preview videos are ffmpeg-generated placeholders, not animals. To get real
footage, see docs/PROVIDERS.md.

EOF
