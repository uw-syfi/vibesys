#!/usr/bin/env bash
# Run the Python checks CI runs, from the same policy CI reads
# (.repoctl/checks.toml), stopping at the first failing command.
# Run this before opening a PR. Needs Go and uv.
set -euo pipefail
cd "$(dirname "$0")/.."

CHECK_GROUPS=(python_quality python_types)

for group in "${CHECK_GROUPS[@]}"; do
  echo "== check group: ${group}"
  ./support/repoctl/repoctl run-checks --group "${group}"
done
