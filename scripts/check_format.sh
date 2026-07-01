#!/usr/bin/env bash
set -euo pipefail

uv run ruff check --select I src tests
uv run ruff format --check src tests
