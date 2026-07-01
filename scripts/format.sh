#!/usr/bin/env bash
set -euo pipefail

targets=(src tests examples resources)

uv run ruff check --select I --fix "${targets[@]}"
uv run ruff format "${targets[@]}"
