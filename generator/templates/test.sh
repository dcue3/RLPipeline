#!/bin/bash
# __CANARY__
#
# Runs the checks and writes the reward. Harbor reads /logs/verifier/reward.txt:
# 1 means every test passed, 0 means at least one failed.

mkdir -p /logs/verifier
# Default to failure so a reward always exists, even if the tooling below breaks.
echo 0 > /logs/verifier/reward.txt

# Tests always run from /app, whatever the image's last WORKDIR was.
cd /app 2>/dev/null || true

# The tests run in their own Python 3.13, separate from anything in the task
# image, so they only rely on the standard library and pytest. That Python's
# directory is put first on PATH; tests/conftest.py restores the image's own
# PATH from this variable so that programs the tests launch (python3, pip,
# node ...) are the image's, exactly as the agent used them.
export TASK_ORIGINAL_PATH="$PATH"
run_tests() {
  "$1" \
    -p 3.13 \
    -w pytest==8.4.1 \
    -w pytest-json-ctrf==0.3.5 \
    pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
}

STATUS=1
if [ -x /opt/verifier/bin/uvx ]; then
  # Tooling installed when the image was built: fast, and needs no network.
  UV_OFFLINE=1 UV_CACHE_DIR=/opt/verifier/cache UV_PYTHON_INSTALL_DIR=/opt/verifier/python \
    run_tests /opt/verifier/bin/uvx
  STATUS=$?
fi

if [ ! -s /logs/verifier/ctrf.json ]; then
  # No report: the pre-installed tooling is missing or damaged. Fetch it afresh.
  if ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y curl
  fi
  curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
  source "$HOME/.local/bin/env"
  run_tests uvx
  STATUS=$?
fi

if [ $STATUS -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
