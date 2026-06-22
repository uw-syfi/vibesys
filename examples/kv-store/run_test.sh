#!/usr/bin/env bash
# End-to-end test: starts the seed server, runs checker + benchmark, cleans up.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
PORT=6399

cleanup() { kill $SERVER_PID 2>/dev/null || true; }
trap cleanup EXIT

$PYTHON reference/seed_server.py $PORT &
SERVER_PID=$!
sleep 0.5

echo "=== ACCURACY CHECK ==="
$PYTHON accuracy_checker/checker.py --port $PORT

echo ""
echo "=== BENCHMARK ==="
$PYTHON benchmark/benchmark.py --port $PORT --num-ops 5000 --num-keys 500
