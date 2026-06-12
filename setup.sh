#!/usr/bin/env bash
# Create the project virtual env and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate

pip install --upgrade pip --quiet
pip install -r requirements.txt

echo
echo "Virtual environment ready. Activate it with:"
echo "  source .venv/bin/activate"
echo ""
echo "Then run the planner with:"
echo "  python hoffroute.py <map.pdf> -o route_out"
echo "  uvicorn webapp:app --port 8000"
