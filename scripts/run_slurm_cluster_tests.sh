#!/usr/bin/env bash
# Run the real-Slurm integration tier: the `slurm` and `slurm-gpu` run environments
# against a Slurm cluster (munge, slurmctld, slurmdbd, one slurmd) started in Docker.
# No LLM agents are involved; the agent side is a Docker container running scripted
# commands. See "Real-cluster tier" in docs/contributing/development.md.
#
#   scripts/run_slurm_cluster_tests.sh                  # the whole tier
#   scripts/run_slurm_cluster_tests.sh -k cancellation  # extra pytest arguments
#
# Needs Docker. The cluster's shared directory is bind-mounted into containers, so
# it must be on a local filesystem the Docker daemon can mount: set
# VIBESYS_SLURM_CLUSTER_DIR to such a directory (default: a fresh directory under
# $TMPDIR or /tmp, which is wrong on hosts where /tmp is a network mount).
# Containers and networks the tier creates carry the label
# io.vibesys.slurm-cluster-test and are removed on exit, even after an interrupt.
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
if [ -n "${VIBESYS_SLURM_CLUSTER_DIR:-}" ]; then
  mkdir -p "$VIBESYS_SLURM_CLUSTER_DIR"
  work="$(mktemp -d "$VIBESYS_SLURM_CLUSTER_DIR/run-XXXXXX")"
else
  work="$(mktemp -d "${TMPDIR:-/tmp}/vibesys-slurm-cluster-XXXXXX")"
fi
label="io.vibesys.slurm-cluster-test"

cleanup() {
  # Only objects this tier labelled; the test fixtures remove their own on a clean exit.
  local ids
  ids="$(docker ps -aq --filter "label=$label" 2>/dev/null || true)"
  if [ -n "$ids" ]; then docker rm -f -v $ids >/dev/null 2>&1 || true; fi
  local networks
  networks="$(docker network ls -q --filter "label=$label" 2>/dev/null || true)"
  if [ -n "$networks" ]; then docker network rm $networks >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

VIBESYS_SLURM_CLUSTER=1 uv run --project "$root" pytest \
  "$root/tests/slurm_cluster" \
  -p no:cacheprovider --no-cov -rsx --durations=15 --basetemp="$work/pytest" "$@"
