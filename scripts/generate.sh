#!/usr/bin/env bash
# Generate a single task from a topic string.
# Usage: bash scripts/generate.sh "fix an off-by-one in a Go pagination helper"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ $# -lt 1 ]; then
    echo "Usage: bash scripts/generate.sh <topic>"
    echo "Example: bash scripts/generate.sh \"fix an off-by-one in a Go pagination helper\""
    exit 1
fi

cd "$ROOT_DIR"
PYTHON="$ROOT_DIR/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON=python
"$PYTHON" -m generator.generate "$@"
