#!/usr/bin/env bash
# Run a command with gateway credentials taken ONLY from the repo's .env file.
# Usage: bash scripts/with_env.sh harbor run -p output/<task> -a terminus-2 ...
#
# Any OPENROUTER_*/OPENAI_* variables already present in the calling shell are
# discarded first, so a personal provider key can never be sent to the LLM provider
# (or the LLM provider key to another provider) by accident.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE (copy .env.example and add LLM_API_KEY)." >&2
    exit 1
fi

unset OPENROUTER_API_KEY OPENROUTER_API_BASE OPENAI_API_KEY OPENAI_BASE_URL

KEY="$(grep -E '^LLM_API_KEY=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]")"
BASE="$(grep -E '^LLM_API_BASE=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d "\"'[:space:]" || true)"

if [ -z "$KEY" ]; then
    echo "LLM_API_KEY is empty in $ENV_FILE." >&2
    exit 1
fi

# Harbor's LiteLLM routes `openrouter/...` models through these two variables.
export OPENROUTER_API_KEY="$KEY"
export OPENROUTER_API_BASE="${BASE:-https://openrouter.ai/api/v1}"

exec "$@"
