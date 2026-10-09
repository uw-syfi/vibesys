#!/usr/bin/env bash
# Run the dynamic-loop smoke tier: the real `vibesys` CLI, real agent CLIs, and
# the Fake Slurm cluster. Run it before every live hardware run.
#
#   scripts/smoke_dynamic_loop.sh                 # Claude Haiku, both scenarios
#   VIBESYS_SMOKE_PROVIDER=codex scripts/smoke_dynamic_loop.sh
#   scripts/smoke_dynamic_loop.sh -k invariants   # extra pytest arguments
#
# Run outputs land in $VIBESYS_SMOKE_DIR (default: a fresh vibesys-smoke-*
# directory under $TMPDIR, or /tmp); smoke-summary.txt there has one line per
# run and its invariant violations. The directory must be outside the checkout:
# the runs create input projects under it, and a project nested in another Git
# repository is rejected.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
if [ -n "${VIBESYS_SMOKE_DIR:-}" ]; then
  out="$VIBESYS_SMOKE_DIR"
  mkdir -p "$out"
else
  out="$(mktemp -d "${TMPDIR:-/tmp}/vibesys-smoke-XXXXXX")"
fi
# Compare physical paths so a symlinked temp root (macOS /var) cannot hide nesting.
out="$(cd "$out" && pwd -P)"
physical_root="$(cd "$root" && pwd -P)"
case "$out/" in
  "$physical_root"/*)
    echo "smoke output directory $out is inside the checkout $physical_root;" \
      "choose one outside it (VIBESYS_SMOKE_DIR or TMPDIR)" >&2
    exit 2
    ;;
esac
status=0
VIBESYS_E2E_AGENTS=1 uv run --project "$root" pytest \
  "$root/tests/e2e/test_dynamic_loop_smoke_e2e.py" \
  -p no:cacheprovider --no-cov -rs --basetemp="$out" "$@" || status=$?
if [ -f "$out/smoke-summary.txt" ]; then
  cat "$out/smoke-summary.txt"
fi
exit "$status"
