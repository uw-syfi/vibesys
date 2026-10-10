#!/usr/bin/env bash
# Run the minimal-container integration tier: every process that runs in the editor
# container (the agent CLIs, the evaluation tool server, the profiler MCP server and
# the broker clients) started in a real container built from the real agent image on
# each default base image (CPU, CUDA, ROCm), on a host without GPUs. No LLM and no GPU
# is involved. See "Minimal-container tier" in docs/contributing/development.md.
#
#   scripts/run_minimal_container_tests.sh                       # every base image
#   VIBESYS_MINIMAL_CONTAINER_BASES=cpu scripts/run_minimal_container_tests.sh
#   scripts/run_minimal_container_tests.sh -k profiler           # extra pytest arguments
#
# Needs Docker. The first run pulls the CUDA and ROCm base images (about 25 GB each) and
# builds the agent layer on each; later runs reuse Docker's cache. An image is pulled
# only when the local store lacks it, and none is removed. The run directory is
# bind-mounted into containers, so it must be on a local filesystem the Docker daemon
# can mount: set VIBESYS_MINIMAL_CONTAINER_DIR to such a directory (default: a fresh
# directory under $TMPDIR or /tmp). Containers the tier starts carry a vibesys.run-id
# label starting with minimal-container- and are removed on exit, even after an interrupt.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
if [ -n "${VIBESYS_MINIMAL_CONTAINER_DIR:-}" ]; then
  mkdir -p "$VIBESYS_MINIMAL_CONTAINER_DIR"
  work="$(mktemp -d "$VIBESYS_MINIMAL_CONTAINER_DIR/run-XXXXXX")"
else
  work="$(mktemp -d "${TMPDIR:-/tmp}/vibesys-minimal-container-XXXXXX")"
fi

cleanup() {
  # Only containers whose run id the tier chose; the fixtures remove their own on a clean exit.
  local ids
  ids="$(docker ps -a --filter "label=vibesys.run-id" --format '{{.ID}} {{.Label "vibesys.run-id"}}' 2>/dev/null \
    | awk '$2 ~ /^minimal-container-/ {print $1}' || true)"
  if [ -n "$ids" ]; then docker rm -f -v $ids >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

VIBESYS_MINIMAL_CONTAINER=1 uv run --project "$root" pytest \
  "$root/tests/minimal_container" \
  -p no:cacheprovider --no-cov -rsx --durations=10 --basetemp="$work/pytest" "$@"
