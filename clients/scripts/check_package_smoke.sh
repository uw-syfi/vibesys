#!/usr/bin/env bash
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"

pack_dir="$RUNNER_TEMP/vibesys-tui-pack"
install_dir="$RUNNER_TEMP/vibesys-tui-install"
mkdir -p "$pack_dir" "$install_dir"
pnpm --dir tui pack --pack-destination "$pack_dir"
npm install --prefix "$install_dir" --ignore-scripts "$pack_dir"/*.tgz
test -f "$install_dir/node_modules/@vibesys/tui/node_modules/@vibesys/backend-client/dist/index.js"
test -f "$install_dir/node_modules/@vibesys/tui/node_modules/@vibesys/core-state/dist/index.js"
bun "$install_dir/node_modules/@vibesys/tui/dist/self-test.js"

deploy_dir="$RUNNER_TEMP/vibesys-tui-deploy"
pnpm --config.node-linker=hoisted --filter @vibesys/tui deploy --prod "$deploy_dir"
test -f "$deploy_dir/node_modules/@vibesys/backend-client/dist/index.js"
test -f "$deploy_dir/node_modules/@vibesys/core-state/dist/index.js"
bun "$deploy_dir/dist/self-test.js"
