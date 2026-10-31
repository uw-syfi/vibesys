#!/usr/bin/env bash
# Runs the app shell's Playwright suite in the Linux image Playwright publishes for the version the
# workspace pins, on x86-64: the browser and OS the `clients_e2e` CI job uses (Chromium on
# ubuntu-latest). The layout and wording checks read real layout, which differs a little between
# macOS and Linux text rendering, so a check that passes here passes in CI. Needs Docker. Arguments
# go to `playwright test`; `SCREENS_DIR=<dir under clients/>` writes the contact sheet there.
#
#   pnpm --filter @vibesys/app-desktop-shell e2e:linux
#   pnpm --filter @vibesys/app-desktop-shell e2e:linux screens
#
# The native replays are not in the tree: the package script starts this through
# with_derived_replays.mjs, which makes the set on the host (the image has no uv or Python) and
# names it in VIBESYS_DERIVED_REPLAYS. It is mounted read-only. A spec that builds a fixture
# differing from its declaration needs that fixture's replay projected once by a run on the host
# (`pnpm e2e <spec>`), which keeps it in the same set for this run to read.
set -euo pipefail

: "${VIBESYS_DERIVED_REPLAYS:?run this as pnpm e2e:linux, which makes the derived replays on the host first}"

shell="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clients="$(cd "$shell/../.." && pwd)"
manifest="$clients/node_modules/@playwright/test/package.json"
version="$(node -p "JSON.parse(require('fs').readFileSync('$manifest', 'utf8')).version")"

(cd "$shell" && pnpm build)
docker run --rm --platform linux/amd64 --ipc=host \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e CI= -e SCREENS_DIR="${SCREENS_DIR:-}" \
  -e VIBESYS_DERIVED_REPLAYS=/derived -v "$VIBESYS_DERIVED_REPLAYS:/derived:ro" \
  -v "$clients:/clients" -w /clients/apps/desktop-shell \
  "mcr.microsoft.com/playwright:v${version}-noble" \
  node --conditions=@vibesys/source /clients/node_modules/@playwright/test/cli.js test "$@"
