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
# uv's own request timeout is 30 s; state it so the bound is visible here.
export UV_HTTP_TIMEOUT=60

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

UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install retry uv python install --no-cache "${PYTHON_VERSION}"
UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv venv --no-cache \
    --python "${PYTHON_VERSION}" --python-preference only-managed "${PYTHON_HOME}"
retry uv pip install --no-cache --python "${PYTHON_HOME}/bin/python" 'mcp>=1.0,<2'
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
