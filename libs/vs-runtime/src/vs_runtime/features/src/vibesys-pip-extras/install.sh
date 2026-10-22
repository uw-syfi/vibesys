#!/usr/bin/env bash
# Run-environment pip extras as a Dev Container Feature. Self-contained: installs
# the Python packaging tools it uses and depends on no other Feature. Installs
# with the `pip` the base image puts first on PATH (a conda `pip` on the ROCm
# PyTorch base), exactly as the agent image always has.
set -euo pipefail

# >>> vibesys network helpers (identical in every Feature; Features cannot share files)
# Every fetch in a Feature is bounded and retried a bounded number of times, so
# a stalled connection ends in a failure that names the step instead of hanging
# the image build until the caller's budget runs out. Only operations that are
# safe to repeat are retried: downloads to a path and package installs.
NET_ATTEMPTS=5
retry() {
    local attempt=1
    until "$@"; do
        if [ "${attempt}" -ge "${NET_ATTEMPTS}" ]; then
            echo "giving up after ${attempt} attempts: $*" >&2
            return 1
        fi
        echo "retry ${attempt}/${NET_ATTEMPTS} after failure: $*" >&2
        sleep $((attempt * 5))
        attempt=$((attempt + 1))
    done
}
# Fail a transfer that stalls (under 10 KB/s for 30 s) or runs over 10 minutes.
fetch() {
    retry curl -fsSL --connect-timeout 20 --speed-limit 10240 --speed-time 30 --max-time 600 "$@"
}
# Per-request cap. A transfer that keeps sending stays under it, so the update
# also has a total deadline (APT_UPDATE_DEADLINE).
APT_BOUNDS=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2)
# GPU bases ship NVIDIA CUDA or AMD ROCm lists. Their indexes are large, and a
# slow transfer never trips Acquire::http::Timeout, so one update can run until
# the CI job is cancelled. Features install only distribution packages.
APT_UPDATE_DEADLINE=60
drop_vendor_apt_sources() {
    local dir=/etc/apt/sources.list.d
    [ -d "${dir}" ] || return 0
    find "${dir}" -type f \( \
        -iname '*nvidia*' -o -iname '*cuda*' -o -iname '*rocm*' -o -iname '*amdgpu*' \
        -o -exec grep -Eqi 'nvidia\.com|repo\.radeon\.com|rocm|amdgpu' {} \; \
    \) -delete
}
apt_update_and_install() {
    drop_vendor_apt_sources
    timeout "${APT_UPDATE_DEADLINE}" apt-get update -qq "${APT_BOUNDS[@]}" &&
        apt-get install -y -qq --no-install-recommends "${APT_BOUNDS[@]}" "$@"
}
apt_install() {
    retry apt_update_and_install "$@"
}
# npm's own defaults (a 300 s request timeout, 2 retries) outlast a CI job's budget.
npm_install_global() {
    retry npm install -g --include=optional --fetch-retries=3 --fetch-timeout=60000 \
        --fetch-retry-mintimeout=5000 --fetch-retry-maxtimeout=30000 "$@"
}
pip_install() {
    retry pip install --no-cache-dir --retries 5 --timeout 30 "$@"
}
# <<< vibesys network helpers

if [ -z "${REQUIREMENTS:-}" ]; then
    echo "vibesys-pip-extras: option 'requirements' is required" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt_install ca-certificates python3 python3-pip
rm -rf /var/lib/apt/lists/*

# shellcheck disable=SC2086  # REQUIREMENTS is a space-separated requirement list.
PIP_BREAK_SYSTEM_PACKAGES=1 pip_install ${REQUIREMENTS}
