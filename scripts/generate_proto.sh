#!/usr/bin/env bash
# Local, version-pinned plugins only. Install clients dependencies before running.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv run --locked --group proto buf generate
# Normalize the plugin's extra EOF blank lines without editing generated code by hand.
uv run --locked --group proto python - <<'PYTHON'
from pathlib import Path
for path in Path("clients/libs/proto/src/generated").rglob("*.ts"):
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
PYTHON
uv run --locked --group proto python scripts/generate_proto_framing.py
node clients/libs/proto/generate-schema-files.mjs
