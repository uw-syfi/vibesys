#!/usr/bin/env bash
# Run the tiny-model CPU check from the candidate root:
#   .vibesys/tasks/default/cpu_check/run.sh [--expect-cache-hits]
#
# The CPU-torch environment lives in the user cache, not in cpu_check/.venv:
# trusted evaluation stages the whole candidate directory to the GPU node, and
# a ~1 GB environment inside it would slow every evaluation.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export UV_PROJECT_ENVIRONMENT="${QWEN35_CPU_CHECK_VENV:-${XDG_CACHE_HOME:-$HOME/.cache}/vibesys/qwen35-cpu-check-venv}"
# The check imports `reference` and `accuracy_checker` from the task directory; the
# candidate root (the working directory) supplies `engine`.
export PYTHONPATH="$here/..${PYTHONPATH:+:$PYTHONPATH}"
exec uv run --quiet --frozen --project "$here" python -m cpu_check "$@"
