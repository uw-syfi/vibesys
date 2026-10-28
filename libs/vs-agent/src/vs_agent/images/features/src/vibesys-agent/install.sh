#!/usr/bin/env bash
# The VibeSys agent layer as a Dev Container Feature: Node from the nodejs.org tarball, the four provider CLIs,
# uv, VibeSys's uv-managed Python with `mcp`, and the non-root `agent` user.
# Self-contained: installs every package it uses and depends on no other Feature.
set -euo pipefail

# >>> vibesys network helpers (identical in every Feature; Features cannot share files)
# Every fetch in a Feature is bounded and retried a bounded number of times, so
# a stalled connection ends in a failure that names the step instead of hanging
# the image build until the caller's budget runs out. Only operations that are
# safe to repeat are retried: downloads to a path and package installs.
NET_ATTEMPTS=3
NET_DEADLINE=120
retry() {
    local attempt=1 status
    while true; do
        echo "network start ${attempt}/${NET_ATTEMPTS} (limit ${NET_DEADLINE}s): $*" >&2
        if timeout --kill-after=5 "${NET_DEADLINE}" "$@"; then
            echo "network done: $*" >&2
            return 0
        else
            status=$?
        fi
        echo "network failed (exit ${status}): $*" >&2
        if [ "${attempt}" -ge "${NET_ATTEMPTS}" ]; then
            echo "giving up after ${attempt} attempts: $*" >&2
            return "${status}"
        fi
        sleep $((attempt * 5))
        attempt=$((attempt + 1))
    done
}
# Bound connection setup, stalled transfers, and total download time.
fetch() {
    retry curl -fsSL --connect-timeout 20 --speed-limit 10240 --speed-time 30 --max-time "${NET_DEADLINE}" "$@"
}
# Per-request cap. A transfer that keeps sending stays under it, so the update
# also has a total deadline (APT_UPDATE_DEADLINE).
APT_BOUNDS=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2)
# GPU bases ship NVIDIA CUDA or AMD ROCm lists. Their indexes are large, and a
# slow transfer never trips Acquire::http::Timeout, so one update can run until
# the CI job is cancelled. Features install only distribution packages.
# Two update attempts (30s each), two install attempts (45s each), one 5s
# backoff per phase, and four 5s kill graces fit within 180s in total.
APT_ATTEMPTS=2
APT_UPDATE_DEADLINE=30
APT_INSTALL_DEADLINE=45
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
    NET_ATTEMPTS="${APT_ATTEMPTS}" NET_DEADLINE="${APT_UPDATE_DEADLINE}" \
        retry apt-get update -qq "${APT_BOUNDS[@]}" &&
        NET_ATTEMPTS="${APT_ATTEMPTS}" NET_DEADLINE="${APT_INSTALL_DEADLINE}" \
        retry apt-get install -y -qq --no-install-recommends "${APT_BOUNDS[@]}" "$@"
}
apt_install() {
    local package missing=()
    for package in "$@"; do
        if [ "$(dpkg-query -W -f='${Status}' "${package}" 2>/dev/null)" != 'install ok installed' ]; then
            missing+=("${package}")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        echo "apt packages already installed; skipping update and install: $*" >&2
        return 0
    fi
    apt_update_and_install "${missing[@]}"
}
# npm's own defaults (a 300 s request timeout, 2 retries) outlast a CI job's budget.
npm_install_global() {
    local package
    for package in "$@"; do
        retry npm install -g --include=optional --prefer-offline --no-audit --no-fund \
            --fetch-retries=1 --fetch-timeout=30000 \
            --fetch-retry-mintimeout=5000 --fetch-retry-maxtimeout=10000 "${package}" || return $?
    done
}
pip_install() {
    retry pip install --retries 1 --timeout 30 "$@"
}
# <<< vibesys network helpers

require_option() {
    local name="$1" value="$2"
    if [ -z "${value}" ]; then
        echo "vibesys-agent: option '${name}' is required" >&2
        exit 1
    fi
}
require_option node_version "${NODE_VERSION:-}"
require_option claude_version "${CLAUDE_VERSION:-}"
require_option codex_version "${CODEX_VERSION:-}"
require_option gemini_version "${GEMINI_VERSION:-}"
require_option opencode_version "${OPENCODE_VERSION:-}"
require_option python_version "${PYTHON_VERSION:-}"
require_option python_home "${PYTHON_HOME:-}"

export DEBIAN_FRONTEND=noninteractive
# Bound uv connection/read waits as well as the whole command in retry.
export UV_HTTP_CONNECT_TIMEOUT=20 UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=1

# Archive mirrors reachable from our hosts fail transiently often enough to
# abort a single-shot `apt-get update`, so retry.
apt_install curl ca-certificates git ripgrep python3 python3-pip python3-venv tar unzip build-essential
rm -rf /var/lib/apt/lists/*

arch="$(dpkg --print-architecture)"
case "${arch}" in
    amd64) node_arch=x64 ;;
    arm64) node_arch=arm64 ;;
    *) echo "unsupported architecture for Node: ${arch}" >&2; exit 1 ;;
esac
fetch -o /tmp/node.tar.gz \
    "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz"
tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1
rm -f /tmp/node.tar.gz
node --version
npm --version

npm_install_global \
    "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}" \
    "@google/gemini-cli@${GEMINI_VERSION}" \
    "opencode-ai@${OPENCODE_VERSION}"

PIP_BREAK_SYSTEM_PACKAGES=1 pip_install uv

UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install retry uv python install "${PYTHON_VERSION}"
# The Feature may be reapplied to an image that already includes it. Rebuild
# this Feature-owned environment from the selected Python and dependencies.
UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv venv --offline --clear \
    --python "${PYTHON_VERSION}" --python-preference only-managed "${PYTHON_HOME}"
retry uv pip install --python "${PYTHON_HOME}/bin/python" 'mcp>=1.0,<2'
chmod -R a+rX /opt/vibesys-python-install "${PYTHON_HOME}"
"${PYTHON_HOME}/bin/python" -c 'import mcp, pydantic, sys; print(sys.version)'

# The sandbox remaps uid 1000 to the host uid at container start, so the fixed
# uid matters, not the name. Reuse a base image's uid-1000 account by renaming
# it, and rename its primary group too (`jenkins` in the ROCm PyTorch image).
if id -u agent >/dev/null 2>&1; then
    :
elif getent passwd 1000 >/dev/null 2>&1; then
    existing="$(getent passwd 1000 | cut -d: -f1)"
    usermod --login agent --home /home/agent --move-home --shell /bin/bash "${existing}"
else
    useradd --create-home --uid 1000 --shell /bin/bash agent
fi
gid="$(id -g agent)"
if ! getent group agent >/dev/null 2>&1; then
    if getent group "${gid}" >/dev/null 2>&1; then
        groupmod --new-name agent "$(getent group "${gid}" | cut -d: -f1)"
    else
        groupadd --gid "${gid}" agent
    fi
fi

mkdir -p /workspace
chown agent /workspace
