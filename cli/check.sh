#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

unformatted="$(gofmt -l .)"
if [[ -n "$unformatted" ]]; then
  printf '%s\n' "$unformatted" >&2
  printf 'Run gofmt -w on the files above, then rerun cli_checks.\n' >&2
  exit 1
fi

go vet ./...

# Compile every package's tests for each release target, so a symbol missing on
# another OS fails here and not only in CI. This runs before the tests because the
# release build test builds all four targets, and finds their standard library already
# compiled.
# The host target was vetted above, with the same compiled packages.
host="$(go env GOOS)/$(go env GOARCH)"
for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  [[ "$target" == "$host" ]] && continue
  GOOS="${target%/*}" GOARCH="${target#*/}" go vet ./...
done

# Every package's tests run under the race detector, since the daemon's flows are concurrent and
# a package added later is covered without being listed here. The race run includes the tests of the
# release build and install.sh, which run real tools. The per-package timeout only guards against a
# hang; the slowest package, the seeded simulation, takes about a minute under the detector.
go test -race -count=1 -timeout 300s ./...

# More seeds without the detector, since a race the detector finds needs a schedule the first few
# hundred seeds may not reach. The count goes past seed 1519, which was the first to lose a cancel
# across recovery, and the run takes about 30 seconds.
VIBESYS_SIM_SEEDS=1530 go test -count=1 -timeout 300s -run TestSeedsHoldTheInvariants ./internal/sim
