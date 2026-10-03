#!/usr/bin/env bash
# Run the dynamic-loop smoke tier: the real `vibesys` CLI, real agent CLIs, and
# the Fake Slurm cluster. Run it before every live hardware run.
#
#   scripts/smoke_dynamic_loop.sh                 # Claude Haiku, both scenarios
#   VIBESYS_SMOKE_PROVIDER=codex scripts/smoke_dynamic_loop.sh
#   scripts/smoke_dynamic_loop.sh -k invariants   # extra pytest arguments
#
# Run outputs land in $VIBESYS_SMOKE_DIR (default .logs/smoke-<timestamp>);
# smoke-summary.txt there has one line per run and its invariant violations.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
out="${VIBESYS_SMOKE_DIR:-$root/.logs/smoke-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$out"
status=0
VIBESYS_E2E_AGENTS=1 uv run --project "$root" pytest \
  "$root/tests/e2e/test_dynamic_loop_smoke_e2e.py" \
  -p no:cacheprovider --no-cov -rs --basetemp="$out" "$@" || status=$?
if [ -f "$out/smoke-summary.txt" ]; then
  cat "$out/smoke-summary.txt"
fi
exit "$status"
