#!/usr/bin/env bash
# Local, version-pinned plugins only. Install clients dependencies before running.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv run --locked --group proto buf generate
uv run --locked --group proto python scripts/generate_proto_framing.py
uv run --locked --group proto buf generate --template buf.gen.core.yaml
uv run --locked --group proto buf generate --template scripts/corevalue/tests/buf.gen.yaml
uv run --locked --group proto buf generate libs/vs-core/tests/proto \
  --config '{"version":"v2","modules":[{"path":"proto"},{"path":"libs/vs-core/tests/proto"}]}' \
  --template libs/vs-core/tests/buf.gen.yaml
uv run --locked --group proto buf generate libs/vs-runtime/tests/proto \
  --config '{"version":"v2","modules":[{"path":"proto"},{"path":"libs/vs-runtime/tests/proto"}]}' \
  --template libs/vs-runtime/tests/buf.gen.yaml
uv run --locked ruff check --select I --fix libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
uv run --locked ruff format libs/vs-core/src/vs_core/_generated libs/vs-runtime/src/vs_runtime/_core_values
# Normalize the plugin's extra EOF blank lines without editing generated code by hand.
uv run --locked --group proto python - <<'PYTHON'
from pathlib import Path
for path in Path("clients/libs/proto/src/generated").rglob("*.ts"):
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
PYTHON
node clients/libs/proto/generate-schema-files.mjs
