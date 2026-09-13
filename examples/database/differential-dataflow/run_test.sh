#!/usr/bin/env bash
# Validate the seed after VibeSys has materialized engine/ and _ref_engine/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f engine/Cargo.toml || ! -f _ref_engine/Cargo.toml ]]; then
  echo "run_test.sh must run from a VibeSys-materialized project containing engine/ and _ref_engine/." >&2
  exit 2
fi

RESULT_JSON="$(mktemp)"
cleanup() { rm -f "$RESULT_JSON"; }
trap cleanup EXIT

echo "=== ACCURACY CHECK ==="
uv run python accuracy_checker/checker.py --strict

echo
echo "=== BENCHMARK CONTRACT ==="
uv run python benchmark/benchmark.py --reps 1 --warmups 0 --output-json "$RESULT_JSON"
uv run python - "$RESULT_JSON" <<'PY'
import json
import sys

result = json.loads(open(sys.argv[1], encoding="utf-8").read())
cpu_seconds = result.get("cpu_seconds")
if not isinstance(cpu_seconds, (int, float)) or cpu_seconds <= 0:
    raise SystemExit("benchmark result must contain a positive numeric cpu_seconds")
print(f"validated cpu_seconds={cpu_seconds}")
PY
