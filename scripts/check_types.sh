#!/usr/bin/env bash
set -euo pipefail

# CI's typecheck job runs this script on Linux. Pin that platform so a macOS
# checkout reports the same diagnostics, including Linux-only APIs such as
# os.waitid, signal.sigtimedwait, and os.pidfd_open.
uv run ty check --python-platform linux

# The product also runs on macOS (the desktop app's local host), and no CI job
# does. typeshed marks every platform-limited standard-library name, so the same
# check against darwin rejects an API macOS lacks wherever it is used directly.
# The real tiers drive Linux containers and clusters, so they are left out; the
# list is the one pytest reads as sim_real_tiers (tests/quality/test_typecheck_platform.py).
if ! uv run ty check --python-platform darwin \
  --exclude tests/e2e --exclude tests/slurm_cluster --exclude tests/minimal_container; then
  cat >&2 <<'MESSAGE'

A name above does not exist on macOS (the darwin check). Do not guard it with a
platform-name branch or suppress the diagnostic. Reach it through a capability
probe (getattr on the module, then a typed refusal when it is absent) in one
module that owns the mechanism, as vs_sim.exit_wait does for waiting on a child's
exit (waitid on Linux, kqueue on macOS), and give the caller the interface.
MESSAGE
  exit 1
fi
