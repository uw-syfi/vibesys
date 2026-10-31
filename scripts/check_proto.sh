#!/usr/bin/env bash
# The proto check group: no network services or remote code generation plugins.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Print each stage's wall and CPU seconds, so a run over its budget names the stage in the job log.
stage() {
  local TIMEFORMAT="proto stage $1: %R s wall, %U s user, %S s sys" status=0
  shift
  # Keep `time` from being the last command, which bash would exec without reporting.
  time "$@" || status=$?
  return "$status"
}
# Hypothesis builds its Unicode tables on first use and keeps them under .hypothesis. A fresh
# checkout has none, so each pytest worker built its own, about 4 s of CPU apiece on a runner.
# Build them once, beside generation, which leaves cores idle.
warm_hypothesis() {
  uv run --locked python -c 'import hypothesis as h, hypothesis.strategies as st; h.given(st.text())(h.settings(max_examples=1, database=None)(lambda s: None))()'
}
stage hypothesis-cache warm_hypothesis &
warm=$!
# A failed stage below ends the script before the final reap; stop the warm-up with it.
trap '[[ -z "$warm" ]] || kill "$warm" 2>/dev/null || true' EXIT
stage layers uv run --locked python -m scripts.check_proto_layers
stage lint uv run --locked --group proto buf lint
stage generate ./scripts/generate_proto.sh
generated=(proto/vibesys/host/v1/admission_receipt_generated.proto gateway/internal/hostoperations/registry_generated.go src/server/host_operation_types_generated.py libs/vs-runtime/tests/generated libs/vs-core/tests/generated libs/vs-proto/src/vs_proto/_generated clients/libs/proto/src/generated gateway/gen
  libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
  scripts/corevalue/tests/generated_pb scripts/corevalue/tests/generated_values scripts/corevalue/tests/generated_boundary)
git diff --exit-code -- "${generated[@]}"
if [[ -n "$(git ls-files --others --exclude-standard -- "${generated[@]}")" ]]; then
  echo 'Untracked generated bindings; run scripts/generate_proto.sh and commit all outputs.' >&2
  exit 1
fi
# Generation is the only writer. Once it passes, these checks read its outputs.
wait "$warm"
warm=
rm -rf clients/libs/proto/dist
pids=()
breaking() { ./scripts/tests/test_proto_breaking.sh && ./scripts/check_proto_breaking.sh; }
stage breaking breaking &
pids+=("$!")
# 1,000 small tests with no dominant one: spread across the cores the checks beside it leave, not one.
stage pytest uv run --locked pytest libs/vs-proto/tests libs/vs-runlog/tests scripts/corevalue/tests scripts/host_operations/tests -n auto --no-cov -q --durations=15 &
pids+=("$!")
go_gen() { (cd gateway && go build ./gen/... && go vet ./gen/...); }
stage go go_gen &
pids+=("$!")
stage hashes ./scripts/check_schema_hashes.sh &
pids+=("$!")
# Reap every check, including on failure, so none outlives this command.
status=0
for pid in "${pids[@]}"; do
  wait "$pid" || status=1
done
exit "$status"
