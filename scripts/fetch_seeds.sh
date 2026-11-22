#!/usr/bin/env bash
# Fetch the public Terminal-Bench 4.0 tasks used as seeds into seeds/terminal-bench-4.
# They are benchmark data and are never committed to this repository.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/seeds/terminal-bench-4"
if [ -d "$DEST/tasks" ]; then
  echo "already present: $DEST"
  exit 0
fi
mkdir -p "$ROOT/seeds"
git clone --depth 1 --branch v4.0.0 https://github.com/harbor-framework/terminal-bench.git "$DEST"
rm -rf "$DEST/.git"
echo "fetched Terminal-Bench 4.0 tasks into $DEST"
