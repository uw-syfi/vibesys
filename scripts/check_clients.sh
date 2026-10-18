#!/usr/bin/env bash
# Runs the client checks CI runs for a change under clients/, from one command, so a CI-only
# failure (biome format, knip, a stale gallery screenshot) shows up before the push. Each leg is
# a repoctl check group, the same groups the `tui-*` jobs of .github/workflows/test.yml run:
#
#   tui_quality       biome check (lint and format), knip, TypeScript architecture,
#                     protocol bindings, design tokens, package type checks
#   tui_tests         every package's tests and the tool-script tests
#   ui_gallery_linux  the gallery's screenshots compared in the Linux image CI uses, and a check
#                     that a baseline update is deterministic (needs Docker)
#
# `tests/quality/test_check_clients_matches_ci.py` fails when a group CI runs for clients/ is in
# neither list below, so this command cannot drift from CI. Install it as an opt-in pre-push
# hook with scripts/install_pre_push_hook.sh.
#
#   ./scripts/check_clients.sh            # run every leg, stop at the first failure
#   ./scripts/check_clients.sh --dry-run  # print the commands without running them
set -euo pipefail

# The check groups this command runs, in order.
parity_groups=(tui_quality tui_tests ui_gallery_linux)

# CI groups for clients/ that this command leaves out, with the reason.
#   tui_build          the build is a CI artifact check; the package tests build what they need
#   tui_package_smoke  packs and installs the shipped packages; a release-shape check
#   tui_e2e            needs a display and the Electron runtime; its gallery leg is
#                      ui_gallery_linux above
skipped_groups=(tui_build tui_package_smoke tui_e2e)

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ "${1:-}" == "--dry-run" ]]; then
  for group in "${parity_groups[@]}"; do
    ./support/repoctl/repoctl run-checks --group "$group" --dry-run
  done
  exit 0
fi

for group in "${parity_groups[@]}"; do
  echo "==> $group" >&2
  ./support/repoctl/repoctl run-checks --group "$group"
done
