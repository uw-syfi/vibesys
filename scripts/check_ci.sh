#!/usr/bin/env bash
# Run the Python checks CI runs, from the same policy CI reads
# (.repoctl/checks.toml), stopping at the first failing command.
# Run this before opening a PR. Needs Go and uv.
#
# The last step is CI's patch-coverage gate: it runs the whole suite once (the
# CI shards, unsharded), combines the coverage data, applies the repo-wide and
# per-module floors, then fails if the lines your branch changed are covered
# below the `patch_coverage` threshold. The suite takes about 7 minutes on a busy
# 64-core host (30,000 lines of output, kept in a log). The step is skipped when the
# branch changes no measured source file; CHECK_CI_SKIP_COVERAGE=1 skips it.
set -euo pipefail
cd "$(dirname "$0")/.."

CHECK_GROUPS=(python_quality python_guards python_types)
# The coverage groups, in the order CI runs them (.github/workflows/test.yml):
# the test shard (here the whole suite), the combine step, the patch gate.
COVERAGE_GROUPS=(python_ci_tests_shard python_ci_coverage patch_coverage)
COVERAGE_DATA=coverage-data

for group in "${CHECK_GROUPS[@]}"; do
  echo "== check group: ${group}"
  ./support/repoctl/repoctl run-checks --group "${group}"
done

# Measured source: Python outside any tests directory (`[tool.coverage.run] source`
# measures packages, never tests).
changed_source() {
  local base
  base="$(git merge-base origin/main HEAD)"
  { git diff --name-only "${base}"; git ls-files --others --exclude-standard; } |
    grep -E '\.py$' | grep -vE '(^|/)tests?/' || true
}

# One coverage group with its output in a log: the suite prints a line per test.
# The log's tail is shown when the group passes, and more of it when it fails.
run_logged() {
  local group="$1" log
  log="$(mktemp "${TMPDIR:-/tmp}/check_ci.${group}.XXXXXX.log")"
  echo "== check group: ${group} (log: ${log})"
  if ./support/repoctl/repoctl run-checks --group "${group}" >"${log}" 2>&1; then
    tail -n 12 "${log}"
  else
    tail -n 200 "${log}"
    echo "== ${group} failed; full log: ${log}"
    return 1
  fi
}

if [[ "${CHECK_CI_SKIP_COVERAGE:-0}" == 1 ]]; then
  echo "== patch coverage skipped (CHECK_CI_SKIP_COVERAGE=1)"
elif [[ -z "$(changed_source)" ]]; then
  echo "== patch coverage skipped (no changed source file)"
else
  uv sync --dev --quiet
  rm -rf "${COVERAGE_DATA}"
  mkdir -p "${COVERAGE_DATA}"
  # CI's test shards measure with the `sysmon` core, which is cheaper on Python 3.14.
  # Only the test run writes into the directory the combine step reads.
  COVERAGE_FILE="${COVERAGE_DATA}/.coverage.local" COVERAGE_CORE=sysmon \
    run_logged "${COVERAGE_GROUPS[0]}"
  run_logged "${COVERAGE_GROUPS[1]}"
  echo "== check group: ${COVERAGE_GROUPS[2]}"
  ./support/repoctl/repoctl run-checks --group "${COVERAGE_GROUPS[2]}"
fi
