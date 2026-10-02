#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

if ! command -v pnpm >/dev/null 2>&1; then
  echo "run-web-ui: pnpm is required; install pnpm and try again" >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "run-web-ui: uv is required; install uv and try again" >&2
  exit 1
fi

# Keep a fresh checkout one-command: install the locked client workspace before
# the web helper builds the browser bundle. pnpm makes this a cheap no-op when
# node_modules already matches the lockfile.
pnpm --dir clients install --frozen-lockfile

# Call the browser entrypoint directly. Going through `vibesys` would involve
# the interactive launcher, whose default frontend is the OpenTUI client.
browser_flag="--open"
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  browser_flag="--no-open"
fi
exec uv run python -m entrypoints.web live --demo "$browser_flag" "$@"
