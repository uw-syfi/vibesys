#!/usr/bin/env bash
# The proto check group: no network services or remote code generation plugins.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv run --locked --group proto buf lint
base="${PROTO_BASE_REF:-origin/main}"
# Only the first introduction has no store to compare. A missing ref is an error.
git rev-parse --verify "$base^{commit}" > /dev/null
if git cat-file -e "$base:buf.yaml" 2>/dev/null; then
  uv run --locked --group proto buf breaking --path proto/vibesys/store/v1 --path proto/vibesys/search/v1 --path proto/vibesys/core/v1 \
    --against ".git#ref=$base"
elif git cat-file -e "$base:proto/vibesys/store/v1" 2>/dev/null; then
  echo "Store exists at $base but buf.yaml is missing; restore its breaking-check configuration." >&2
  exit 1
else
  echo "No store schema at $base; establishing the initial additive baseline."
fi
./scripts/generate_proto.sh
generated=(libs/vs-proto/src/vs_proto/_generated clients/libs/proto/src/generated gateway/gen
  libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
  scripts/corevalue/tests/generated_pb scripts/corevalue/tests/generated_values scripts/corevalue/tests/generated_boundary)
git diff --exit-code -- "${generated[@]}"
if [[ -n "$(git ls-files --others --exclude-standard -- "${generated[@]}")" ]]; then
  echo 'Untracked generated bindings; run scripts/generate_proto.sh and commit all outputs.' >&2
  exit 1
fi
uv run --locked pytest libs/vs-proto/tests libs/vs-runlog/tests scripts/corevalue/tests --no-cov -q
pnpm --dir clients --filter @vibesys/proto check
(cd gateway && go build ./gen/... && go vet ./gen/...)
./scripts/check_schema_hashes.sh
