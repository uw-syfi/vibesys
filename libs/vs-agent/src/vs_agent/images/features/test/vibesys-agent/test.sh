#!/usr/bin/env bash
# Run by `devcontainer features test` after the Feature is installed with the
# options' defaults, which equal the vs_agent.provider_policy pins.
set -e
source dev-container-features-test-lib
check "claude runs" claude --version
check "codex runs" codex --version
check "gemini runs" gemini --version
check "opencode runs" opencode --version
check "agent user has uid 1000" bash -c '[ "$(id -u agent)" = 1000 ]'
check "agent group exists" getent group agent
check "VibeSys python imports mcp" /opt/vibesys-python/bin/python -c 'import mcp'
check "workspace is owned by agent" bash -c '[ "$(stat -c %U /workspace)" = agent ]'
reportResults
