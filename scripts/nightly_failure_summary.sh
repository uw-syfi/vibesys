#!/usr/bin/env bash
# Print a Markdown failure summary with exact repro commands for a nightly job.
#
# Usage: scripts/nightly_failure_summary.sh LOG TITLE ENV...
# LOG is the captured pytest output. ENV is the `NAME=value` settings that
# reproduce the job (profile, strength, seed window). Each failed test gets its
# own command; a chaos test narrows CHAOS_SEEDS to that one seed. The workflow
# appends stdout to $GITHUB_STEP_SUMMARY.
set -euo pipefail
log="${1:?usage: nightly_failure_summary.sh LOG TITLE ENV...}"
title="${2:?usage: nightly_failure_summary.sh LOG TITLE ENV...}"
shift 2
env_line="$*"

echo "## ${title} failed"
echo
echo "Commit: \`${GITHUB_SHA:-unknown}\` (run ${GITHUB_RUN_NUMBER:-?}). Check it out first."
echo
failed="$(grep -E '^FAILED ' "$log" | awk '{print $2}' | sort -u || true)"
if [ -z "$failed" ]; then
  echo "No failed test was reported (crash or timeout). Rerun the whole job:"
  echo
  echo '```'
  echo "${env_line} uv run python -m pytest -n auto --dist loadgroup --no-cov -q"
  echo '```'
  exit 0
fi
echo "Repro, one command per failed test:"
echo
echo '```'
while IFS= read -r node; do
  extra=""
  if [[ "$node" =~ \[seed_([0-9]+)\] ]]; then
    extra=" CHAOS_SEEDS=${BASH_REMATCH[1]}"
  fi
  echo "${env_line}${extra} uv run python -m pytest '${node}' --no-cov -p no:randomly"
done <<<"$failed"
echo '```'
