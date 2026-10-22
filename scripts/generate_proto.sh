#!/usr/bin/env bash
# Local, version-pinned plugins only. Install clients dependencies before running.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image="$(mktemp)"
trap 'rm -f "$image"' EXIT
uv run --locked --group proto buf build --as-file-descriptor-set --exclude-source-info -o "$image"
uv run --locked --group proto python -m scripts.host_operations.generate "$image"
uv run --locked --group proto buf format -w --path proto/vibesys/host --path proto/vibesys/api/v1/host_operations.proto
uv run --locked --group proto buf generate
uv run --locked --group proto python -m scripts.host_operations.generate "$image"
uv run --locked ruff check --select I --fix libs/vs-proto/src/vs_proto/_generated/host_operations.py src/server/host_operation_types_generated.py
uv run --locked ruff format libs/vs-proto/src/vs_proto/_generated/host_operations.py src/server/host_operation_types_generated.py
gofmt -w gateway/internal/hostoperations/registry_generated.go
uv run --locked --group proto python scripts/generate_proto_framing.py
uv run --locked --group proto buf generate --template buf.gen.core.yaml
uv run --locked --group proto buf generate --template scripts/corevalue/tests/buf.gen.yaml
uv run --locked ruff check --select I --fix libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
uv run --locked ruff format libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
# Normalize the plugin's extra EOF blank lines without editing generated code by hand.
uv run --locked --group proto python - <<'PYTHON'
from pathlib import Path
for path in Path("clients/libs/proto/src/generated").rglob("*.ts"):
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
PYTHON
node clients/libs/proto/generate-schema-files.mjs
