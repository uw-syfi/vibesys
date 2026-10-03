#!/usr/bin/env bash
# Sweep the dynamic-loop chaos harness over seeds FIRST..FIRST+N-1.
#
# Usage: scripts/chaos_dynamic_loop.sh N [FIRST] [WORKERS]
# Each failing seed prints its injected faults, violations, and a repro line.
# One JSON summary per seed is appended to .logs/chaos-<FIRST>-<LAST>.jsonl.
set -euo pipefail
count="${1:?usage: chaos_dynamic_loop.sh N [FIRST] [WORKERS]}"
first="${2:-1000}"
workers="${3:-8}"
last=$((first + count - 1))
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/.logs"
export CHAOS_SEEDS="${first}-${last}"
export CHAOS_LOG="$root/.logs/chaos-${first}-${last}.jsonl"
cd "$root"
exec uv run pytest tests/vibesys/orchestration/dynamic/loop/test_chaos.py \
  -n "$workers" -p no:randomly --no-cov -q
