#!/usr/bin/env bash
set -euo pipefail

# CI's typecheck job runs this script on Linux. Pin that platform so a macOS
# checkout reports the same diagnostics, including Linux-only APIs such as
# os.waitid, signal.sigtimedwait, and os.pidfd_open.
uv run ty check --python-platform linux
