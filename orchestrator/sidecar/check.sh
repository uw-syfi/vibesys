#!/usr/bin/env bash
# The sidecar's own checks: lint, format, and its tests (the unit tests, the store fuzz targets and
# the conformance scenarios over a real Unix socket). Needs `uv sync --group dev` to have run once.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

uv run --locked --group dev ruff check .
uv run --locked --group dev ruff format --check .
uv run --locked --group dev pytest -q -p no:cacheprovider
