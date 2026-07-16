#!/usr/bin/env bash
# End-to-end smoke test. Each trusted evaluator owns candidate lifecycle.
#
# This is a plumbing check for CI and local harness verification, not the
# scored optimization contract. Latency/throughput gates here are intentionally
# looser than vibeserve.input.toml so shared CI VMs (noisy, single-client,
# short runs) do not fail a healthy seed.
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
  --min-throughput-ops-per-sec 1000 \
  --max-read-p99-ms 10 \
  --max-update-p99-ms 10 \
  --output-json "$OUTPUT_JSON"

uv run python - "$OUTPUT_JSON" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
score = payload.get("ops_per_cpu_sec")
if not payload.get("score_valid") or not isinstance(score, (int, float)) or score <= 0:
    raise SystemExit(
        f"smoke benchmark did not produce a valid ops_per_cpu_sec score: {payload!r}"
    )
print(f"Smoke score OK: {score:.1f} ops_per_cpu_sec")
PY
