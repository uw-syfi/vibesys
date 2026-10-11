#!/usr/bin/env bash
# The VibeSys agent layer as a Dev Container Feature: Node from the nodejs.org tarball, the four provider CLIs,
# uv, VibeSys's uv-managed Python with `mcp`, and the non-root `agent` user.
# Self-contained: installs every package it uses and depends on no other Feature.
set -euo pipefail

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

# Archive mirrors reachable from our hosts fail transiently often enough to
# abort a single-shot `apt-get update`, so retry.
installed=""
for attempt in 1 2 3 4 5; do
    if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates git ripgrep python3 python3-pip python3-venv \
        tar unzip build-essential; then
        installed=1
        break
    fi
    echo "apt retry ${attempt}..." >&2
    sleep $((attempt * 5))
done
[ -n "${installed}" ]
rm -rf /var/lib/apt/lists/*

arch="$(dpkg --print-architecture)"
case "${arch}" in
    amd64) node_arch=x64 ;;
    arm64) node_arch=arm64 ;;
    *) echo "unsupported architecture for Node: ${arch}" >&2; exit 1 ;;
esac
curl -fsSL --retry 5 --retry-delay 5 -o /tmp/node.tar.gz \
    "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz"
tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1
rm -f /tmp/node.tar.gz
node --version
npm --version

npm install -g --include=optional \
    "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}" \
    "@google/gemini-cli@${GEMINI_VERSION}" \
    "opencode-ai@${OPENCODE_VERSION}"

PIP_BREAK_SYSTEM_PACKAGES=1 pip install --no-cache-dir uv

UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv python install --no-cache "${PYTHON_VERSION}"
UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv venv --no-cache \
    --python "${PYTHON_VERSION}" --python-preference only-managed "${PYTHON_HOME}"
uv pip install --no-cache --python "${PYTHON_HOME}/bin/python" 'mcp>=1.0,<2'
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
