#!/usr/bin/env bash
# Initial candidate launcher. Agents may replace this seed implementation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${1:?usage: ./run.sh <port>}"

exec "${PYTHON:-python3}" "$ROOT/reference/seed_server.py" "$PORT"
