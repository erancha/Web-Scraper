#!/usr/bin/env bash
# run.sh – Run the Scraper Agent (run in WSL)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
if [ ! -d "venv" ]; then
    echo "[ERR] Virtual environment not found. Run ./setup.sh first."
    exit 1
fi
source venv/bin/activate

# Usage: ./run.sh [once|loop] [--dry-run] [--provider <key>]
#   Examples:
#     ./run.sh once --dry-run
#     ./run.sh loop --provider espn-nba
MODE="loop"
if [ "${1:-}" = "once" ] || [ "${1:-}" = "loop" ]; then
    MODE="$1"
    shift
fi

echo "=== Scraper Agent – mode: $MODE $* ==="
python3 scraper.py "$MODE" "$@"
