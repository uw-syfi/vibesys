#!/usr/bin/env bash
# Sweep the skeleton-search chaos harness over seeds FIRST..FIRST+N-1.
#
# Usage: scripts/chaos_loop.sh N [FIRST] [WORKERS]
# Each failing seed prints its injected faults, violations, and a repro line.
# One JSON summary per seed is appended to .logs/chaos-<FIRST>-<LAST>.jsonl.
set -euo pipefail
count="${1:?usage: chaos_loop.sh N [FIRST] [WORKERS]}"
first="${2:-1000}"
workers="${3:-8}"
last=$((first + count - 1))
root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$root/.logs"
export CHAOS_SEED_RANGE="${first}-${last}"
export CHAOS_LOG="$root/.logs/chaos-${first}-${last}.jsonl"
cd "$root"
exec uv run pytest tests/system/chaos/test_chaos.py \
  -n "$workers" -p no:randomly --no-cov -q
