#!/usr/bin/env bash
set -euo pipefail

# CI's typecheck job runs this script on Linux. Pin that platform so a macOS
# checkout reports the same diagnostics, including Linux-only APIs such as
# os.waitid, signal.sigtimedwait, and os.pidfd_open.
check_linux() { uv run ty check --python-platform linux; }

# The product also runs on macOS (the desktop app's local host). typeshed marks
# every standard-library name a platform lacks, so the same check against darwin
# rejects an API macOS lacks wherever product code uses it directly. Only the
# source roots a macOS host loads are checked: tests, and the real tiers that
# drive Linux systems, never run inside the product, and leaving them out roughly
# halves this pass. CI's sandbox-macos job covers behavior (the vs-sim process-wait
# tests); this check covers names. tests/quality/test_typecheck_platform.py ties
# the roots to the ones ty is configured with.
check_darwin() { uv run ty check --python-platform darwin src libs/*/src sdk/*/src; }

# The two passes are independent, so they run side by side (the pre-push gate and CI's
# typecheck job wait for the slower one instead of both). Each writes to its own file and
# the files are printed in order, so the output never interleaves.
linux_log="$(mktemp)"
darwin_log="$(mktemp)"
trap 'rm -f "$linux_log" "$darwin_log"' EXIT
check_linux >"$linux_log" 2>&1 &
linux_pid=$!
check_darwin >"$darwin_log" 2>&1 &
darwin_pid=$!

status=0
wait "$linux_pid" || status=1
cat "$linux_log"
darwin_status=0
wait "$darwin_pid" || darwin_status=1
cat "$darwin_log"
if [[ "$darwin_status" != 0 ]]; then
  cat >&2 <<'MESSAGE'

A name above does not exist on macOS (the darwin check). Do not guard it with a
platform-name branch or suppress the diagnostic. Reach it through a capability
probe (getattr on the module, then a typed refusal when it is absent) in one
module that owns the mechanism, as vs_sim.exit_wait does for waiting on a child's
exit (waitid on Linux, kqueue on macOS), and give the caller the interface.
MESSAGE
  status=1
fi
exit "$status"
