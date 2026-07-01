#!/usr/bin/env bash
set -euo pipefail

targets=(src tests examples resources)

uv run ruff check --select I "${targets[@]}"
uv run ruff format --check "${targets[@]}"
