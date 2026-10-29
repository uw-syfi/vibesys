#!/usr/bin/env bash
# Runs the app shell's Playwright suite in the Linux image Playwright publishes for the version the
# workspace pins, on x86-64: the browser and OS the `clients_e2e` CI job uses (Chromium on
# ubuntu-latest). The layout and wording checks read real layout, which differs a little between
# macOS and Linux text rendering, so a check that passes here passes in CI. Needs Docker. Arguments
# go to `playwright test`; `SCREENS_DIR=<dir under clients/>` writes the contact sheet there.
#
#   pnpm --filter @vibesys/app-desktop-shell e2e:linux
#   pnpm --filter @vibesys/app-desktop-shell e2e:linux screens
set -euo pipefail

shell="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clients="$(cd "$shell/../.." && pwd)"
manifest="$clients/node_modules/@playwright/test/package.json"
version="$(node -p "JSON.parse(require('fs').readFileSync('$manifest', 'utf8')).version")"

(cd "$shell" && pnpm build)
docker run --rm --platform linux/amd64 --ipc=host \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e CI= -e SCREENS_DIR="${SCREENS_DIR:-}" \
  -v "$clients:/clients" -w /clients/apps/desktop-shell \
  "mcr.microsoft.com/playwright:v${version}-noble" \
  node --conditions=@vibesys/source /clients/node_modules/@playwright/test/cli.js test "$@"
