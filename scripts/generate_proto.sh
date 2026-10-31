#!/usr/bin/env bash
# Local, version-pinned plugins only. Install clients dependencies before running.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image="$(mktemp)"
trap 'rm -f "$image"' EXIT
uv run --locked --group proto buf build --as-file-descriptor-set --exclude-source-info -o "$image"
uv run --locked --group proto python -m scripts.host_operations.generate "$image"
format_args=()
for path in proto/vibesys/host proto/vibesys/api/v1/host_operations.proto proto/vibesys/api/v1/host_read_generated.proto proto/vibesys/api/v1/host_watch_generated.proto proto/vibesys/api/v1/host_mutate_generated.proto; do
  if [[ -e "$path" ]]; then
    format_args+=(--path "$path")
  fi
done
uv run --locked --group proto buf format -w "${format_args[@]}"
uv run --locked --group proto buf generate
uv run --locked --group proto python -m scripts.host_operations.generate "$image" --bindings
uv run --locked ruff check --select I --fix libs/vs-proto/src/vs_proto/_generated/host_operations.py src/server/host_operation_types_generated.py src/server/host_compatibility_generated.py
uv run --locked ruff format libs/vs-proto/src/vs_proto/_generated/host_operations.py src/server/host_operation_types_generated.py src/server/host_compatibility_generated.py
gofmt_paths=()
for path in gateway/internal/hostoperations/registry_generated.go gateway/internal/hostoperations/host_call_generated.go gateway/internal/hostoperations/host_read_generated.go gateway/internal/hostoperations/host_watch_generated.go gateway/internal/hostoperations/host_mutate_generated.go gateway/internal/hostoperations/topics_generated.go; do
  if [[ -f "$path" ]]; then
    gofmt_paths+=("$path")
  fi
done
gofmt -w "${gofmt_paths[@]}"
uv run --locked --group proto python scripts/generate_proto_framing.py
uv run --locked --group proto buf generate --template buf.gen.core.yaml
uv run --locked --group proto buf generate --template scripts/corevalue/tests/buf.gen.yaml
uv run --locked --group proto buf generate libs/vs-core/tests/proto \
  --config '{"version":"v2","modules":[{"path":"proto"},{"path":"libs/vs-core/tests/proto"}]}' \
  --template libs/vs-core/tests/buf.gen.yaml
uv run --locked --group proto buf generate libs/vs-runtime/tests/proto \
  --config '{"version":"v2","modules":[{"path":"proto"},{"path":"libs/vs-runtime/tests/proto"}]}' \
  --template libs/vs-runtime/tests/buf.gen.yaml
uv run --locked --group proto buf generate libs/vs-runtime/tests/proto_prior \
  --config '{"version":"v2","modules":[{"path":"proto"},{"path":"libs/vs-runtime/tests/proto_prior"}]}' \
  --template libs/vs-runtime/tests/buf.prior.gen.yaml
uv run --locked ruff check --select I --fix libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
uv run --locked ruff format libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
# Normalize the plugin's extra EOF blank lines without editing generated code by hand.
uv run --locked --group proto python - <<'PYTHON'
from pathlib import Path
for path in Path("clients/libs/proto/src/generated").rglob("*.ts"):
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
PYTHON
node clients/libs/proto/generate-schema-files.mjs
