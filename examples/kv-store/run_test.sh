#!/usr/bin/env bash
# End-to-end smoke test. Each trusted evaluator owns candidate lifecycle.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

OUTPUT_JSON="$(mktemp)"
trap 'rm -f "$OUTPUT_JSON"' EXIT

echo "=== ACCURACY CHECK ==="
uv run python accuracy_checker/checker.py

echo ""
echo "=== BENCHMARK ==="
uv run python benchmark/benchmark.py \
  --num-keys 500 \
  --duration 2 \
  --repeats 1 \
  --no-warmup \
  --client-procs 1 \
  --saturation-probe-client-procs 1 \
  --max-saturation-gain-pct 100 \
  --output-json "$OUTPUT_JSON"
