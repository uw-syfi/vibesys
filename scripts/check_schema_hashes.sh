#!/usr/bin/env bash
# Compare real runtime hashes with the canonical source image in every language.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
aggregates="$(git ls-files -- \
  libs/vs-proto/src/vs_proto/_generated/schema_hashes.py \
  clients/libs/proto/src/generated/schema-hashes.ts \
  clients/libs/proto/src/generated/schema-files.ts \
  gateway/gen/schema_hashes.go \
  proto/testdata/records/epoch_started.pb)"
if [[ -n "$aggregates" ]]; then
  printf 'Remove committed aggregate schema outputs; derive them locally instead:\n%s\n' "$aggregates" >&2
  exit 1
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
uv run --locked --group proto buf build --as-file-descriptor-set --exclude-source-info -o "$work/image.bin"
uv run --locked python - "$work/image.bin" > "$work/expected" <<'PYTHON'
import sys
from pathlib import Path
from vs_proto.api import schema_hash
image = Path(sys.argv[1]).read_bytes()
for package in ("vibesys.api.v1", "vibesys.store.v1", "vibesys.host.v1"):
    print(schema_hash(image, package))
PYTHON
uv run --locked python - <<'PYTHON' > "$work/python"
from vs_proto.api import API_SCHEMA_HASH, STORE_SCHEMA_HASH, HOST_SCHEMA_HASH
print(API_SCHEMA_HASH)
print(STORE_SCHEMA_HASH)
print(HOST_SCHEMA_HASH)
PYTHON
# The solution config references the library build; check and emit it once.
pnpm --dir clients --filter @vibesys/proto check
node --input-type=module - <<'JS' > "$work/typescript"
import {API_SCHEMA_HASH, STORE_SCHEMA_HASH, HOST_SCHEMA_HASH} from './clients/libs/proto/dist/index.js';
console.log(API_SCHEMA_HASH);
console.log(STORE_SCHEMA_HASH);
console.log(HOST_SCHEMA_HASH);
JS
(cd gateway && go run ./cmd/schema-hashes) > "$work/go"
for language in python typescript go; do
  if ! diff -u "$work/expected" "$work/$language"; then
    echo "$language schema hashes diverge from the buf image; regenerate bindings and check canonicalization." >&2
    exit 1
  fi
done
echo 'Python, TypeScript and Go schema hashes match the canonical buf image.'
