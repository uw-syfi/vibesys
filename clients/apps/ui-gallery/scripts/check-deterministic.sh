#!/usr/bin/env bash
# Fails if two consecutive baseline updates in the Linux image write different images: a baseline
# update must rewrite only the screens whose rendering changed, so the same page must render to the
# same bytes every time (see DETERMINISTIC_RENDERING in playwright.config.ts). The checked-in
# baselines are restored afterwards, so the check leaves the working tree as it found it. Needs
# Docker.
#
#   pnpm --filter @vibesys/app-ui-gallery check:deterministic
set -euo pipefail

gallery="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
shots="$gallery/e2e/__screenshots__"
scratch="$(mktemp -d)"
trap 'rm -rf "$shots"; cp -R "$scratch/original" "$shots"; rm -rf "$scratch"' EXIT

cp -R "$shots" "$scratch/original"
bash "$gallery/scripts/linux-playwright.sh" --update-snapshots >/dev/null
cp -R "$shots" "$scratch/first"
bash "$gallery/scripts/linux-playwright.sh" --update-snapshots >/dev/null

if ! diff -rq "$scratch/first" "$shots"; then
  echo "two consecutive baseline updates wrote different screenshots (listed above)" >&2
  exit 1
fi
echo "two consecutive baseline updates wrote identical screenshots"
