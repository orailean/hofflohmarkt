#!/usr/bin/env bash
# Create the project virtual env and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt

echo
echo "Done. Run the planner with:"
echo "  .venv/bin/python hoffroute.py <map.pdf> --calib calib_haidhausen.json -o route_out"
