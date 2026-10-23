#!/usr/bin/env bash
# The proto check group: no network services or remote code generation plugins.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv run --locked python -m scripts.check_proto_layers
uv run --locked --group proto buf lint
./scripts/generate_proto.sh
generated=(proto/vibesys/host/v1/admission_receipt_generated.proto gateway/internal/hostoperations/registry_generated.go src/server/host_operation_types_generated.py libs/vs-runtime/tests/generated libs/vs-core/tests/generated libs/vs-proto/src/vs_proto/_generated clients/libs/proto/src/generated gateway/gen
  libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
  scripts/corevalue/tests/generated_pb scripts/corevalue/tests/generated_values scripts/corevalue/tests/generated_boundary)
git diff --exit-code -- "${generated[@]}"
if [[ -n "$(git ls-files --others --exclude-standard -- "${generated[@]}")" ]]; then
  echo 'Untracked generated bindings; run scripts/generate_proto.sh and commit all outputs.' >&2
  exit 1
fi
# Generation is the only writer. Once it passes, these checks read its outputs.
pids=()
(./scripts/tests/test_proto_breaking.sh && ./scripts/check_proto_breaking.sh) &
pids+=("$!")
# Two workers: the other checks finish within 20 s, and one worker leaves their cores idle for the
# remaining half of this group's 60 s.
uv run --locked pytest libs/vs-proto/tests libs/vs-runlog/tests scripts/corevalue/tests scripts/host_operations/tests -n 2 --no-cov -q &
pids+=("$!")
(cd gateway && go build ./gen/... && go vet ./gen/...) &
pids+=("$!")
./scripts/check_schema_hashes.sh &
pids+=("$!")
# Reap every check, including on failure, so none outlives this command.
status=0
for pid in "${pids[@]}"; do
  wait "$pid" || status=1
done
exit "$status"
