#!/usr/bin/env bash
# setup.sh – Set up the Scraper Agent environment (run in WSL)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Scraper Agent – Setup ==="

# Check Python version (require 3.8+)
PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3, 8))' 2>/dev/null || echo "False")
if [ "$PYTHON_VERSION" != "True" ]; then
    echo "[ERR] Python 3.8+ is required. Current: $(python3 --version 2>/dev/null || echo 'not found')"
    exit 1
fi

# Install python3-venv if missing
if ! dpkg -s python3-venv &>/dev/null; then
    echo "[*] Installing python3-venv …"
    sudo apt-get update && sudo apt-get install -y python3-venv
fi

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment …"
    python3 -m venv venv
fi

echo "[*] Activating venv & installing dependencies …"
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create .env from example if it doesn't exist
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "[*] Created .env from .env.example – please edit it with your SMTP credentials."
    fi
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Edit .env with your SMTP credentials (SMTP_USER, SMTP_PASS)"
echo "  2. Run:  ./run.sh        (continuous loop)"
echo "  3.   or: ./run.sh once   (single check)"
