#!/usr/bin/env bash
set -euo pipefail

# Match the Linux platform used by CI, including Linux-only process APIs.
uv run ty check --python-platform linux
