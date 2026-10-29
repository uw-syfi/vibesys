#!/usr/bin/env bash
# Runs the gallery's Playwright suite in the Linux image Playwright publishes for the version the
# workspace pins, on x86-64: the browser and OS the `clients_e2e` CI job uses (Chromium on
# ubuntu-latest). The screenshot baselines (e2e/__screenshots__) are written here, never on macOS,
# whose font rasterization differs. Needs Docker. Arguments go to `playwright test`; with
# `--update-snapshots` the old baselines are removed first, so a removed entry leaves none behind.
#
#   pnpm --filter @vibesys/app-ui-gallery update:screenshots   # rewrite the baselines
#   pnpm --filter @vibesys/app-ui-gallery test:e2e:linux       # compare against them
set -euo pipefail

gallery="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
clients="$(cd "$gallery/../.." && pwd)"
manifest="$clients/node_modules/@playwright/test/package.json"
version="$(node -p "JSON.parse(require('fs').readFileSync('$manifest', 'utf8')).version")"

(cd "$gallery" && pnpm build)
if [[ "${1:-}" == "--update-snapshots" ]]; then
  rm -rf "$gallery/e2e/__screenshots__"
fi
docker run --rm --platform linux/amd64 --ipc=host \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -e CI= \
  -v "$clients:/clients" -w /clients/apps/ui-gallery \
  "mcr.microsoft.com/playwright:v${version}-noble" \
  node /clients/node_modules/@playwright/test/cli.js test "$@"
