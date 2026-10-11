#!/usr/bin/env bash
# Run-environment pip extras as a Dev Container Feature. Self-contained: installs
# the Python packaging tools it uses and depends on no other Feature. Installs
# with the `pip` the base image puts first on PATH (a conda `pip` on the ROCm
# PyTorch base), exactly as the agent image always has.
set -euo pipefail

if [ -z "${REQUIREMENTS:-}" ]; then
    echo "vibesys-pip-extras: option 'requirements' is required" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
installed=""
for attempt in 1 2 3 4 5; do
    if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        ca-certificates python3 python3-pip; then
        installed=1
        break
    fi
    echo "apt retry ${attempt}..." >&2
    sleep $((attempt * 5))
done
[ -n "${installed}" ]
rm -rf /var/lib/apt/lists/*

# shellcheck disable=SC2086  # REQUIREMENTS is a space-separated requirement list.
PIP_BREAK_SYSTEM_PACKAGES=1 pip install --no-cache-dir ${REQUIREMENTS}
