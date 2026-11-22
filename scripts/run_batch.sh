#!/usr/bin/env bash
# Run the full pipeline on a batch of candidate tasks.
# Usage: bash scripts/run_batch.sh --run-id batch1 --n 12
# Run the same command again to resume a killed run.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="$ROOT_DIR/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON=python
exec "$PYTHON" -m pipeline.run "$@"
