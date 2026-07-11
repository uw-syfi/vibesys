#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

interactive=true
if [[ ! -t 0 || ! -t 1 ]]; then
  interactive=false
fi
for argument in "$@"; do
  if [[ "$argument" == "--headless" ]]; then
    interactive=false
    break
  fi
done

if [[ "$interactive" == true ]]; then
  entrypoint="clients/tui/dist/index.js"
  rebuild=false
  if [[ ! -f "$entrypoint" ]]; then
    rebuild=true
  elif find \
    clients/tui/src \
    clients/tui/package.json \
    clients/tui/pnpm-lock.yaml \
    clients/tui/tsconfig.json \
    clients/tui/tsconfig.check.json \
    src/vibe_serve/server \
    -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.json' -o -name '*.yaml' -o -name '*.py' \) \
    -newer "$entrypoint" -print -quit | grep -q .; then
    rebuild=true
  fi

  if [[ "$rebuild" == true ]]; then
    if ! command -v node >/dev/null 2>&1 && [[ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then
      # nvm is a shell function, so repository launchers must load it explicitly.
      source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
      nvm use node >/dev/null
    fi
    if ! command -v node >/dev/null 2>&1; then
      echo "vs: Node.js 20+ is required for the interactive client." >&2
      exit 1
    fi

    node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
    if [[ "$node_major" -lt 20 ]]; then
      echo "vs: Node.js 20+ is required; found $(node --version)." >&2
      exit 1
    fi

    if command -v pnpm >/dev/null 2>&1; then
      pnpm_command=(pnpm)
    elif command -v corepack >/dev/null 2>&1; then
      pnpm_command=(corepack pnpm)
    else
      echo "vs: pnpm is required. Install pnpm or enable Corepack." >&2
      exit 1
    fi

    echo "Launching VibeServe..." >&2
    preparation_log="$(mktemp -t vibeserve-prepare.XXXXXX)"
    trap 'rm -f "$preparation_log"' EXIT
    if ! {
      "${pnpm_command[@]}" --dir clients/tui install --frozen-lockfile &&
        "${pnpm_command[@]}" --dir clients/tui generate:protocol &&
        "${pnpm_command[@]}" --dir clients/tui build
    } >"$preparation_log" 2>&1; then
      echo "vs: failed to prepare the interactive client:" >&2
      sed 's/^/  /' "$preparation_log" >&2
      exit 1
    fi
    rm -f "$preparation_log"
    trap - EXIT
  fi
fi

exec uv run vibe-serve "$@"
