#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# The tests open real stores under TMPDIR, and a store refuses a file system whose locks do not work,
# such as NFS mounted with nolock. Where /tmp is such a mount, set TMPDIR to a directory on a local
# disk before running this script.

unformatted="$(gofmt -l .)"
if [[ -n "$unformatted" ]]; then
  printf '%s\n' "$unformatted" >&2
  printf 'Run gofmt -w on the files above, then rerun cli_checks.\n' >&2
  exit 1
fi

# The docker tag adds the tests that drive a real Docker daemon. They are vetted everywhere, so they
# always compile, and run below where a daemon is reachable.
go vet -tags docker ./...

# Compile every package's tests for each release target, so a symbol missing on
# another OS fails here and not only in CI. This runs before the tests because the
# release build test builds all four targets, and finds their standard library already
# compiled.
# The host target was vetted above, with the same compiled packages.
host="$(go env GOOS)/$(go env GOARCH)"
for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  [[ "$target" == "$host" ]] && continue
  GOOS="${target%/*}" GOARCH="${target#*/}" go vet -tags docker ./...
done

# Every package's tests run under the race detector, since the daemon's flows are concurrent and
# a package added later is covered without being listed here. The race run includes the tests of the
# release build and install.sh, which run real tools. The per-package timeout only guards against a
# hang; the slowest package, the seeded simulation, takes about a minute under the detector. The
# lifecycle package includes the exhaustive model check of two jobs (model_test.go), every order of
# events to depth 10 in one store and depth 9 across two, in a few seconds.
go test -race -count=1 -timeout 300s ./...

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # The tests that drive a real Docker daemon pass the same contracts the fakes pass, which is what
  # lets the fakes stand in for Docker in every other test, and the tool as shipped runs a task of a
  # Dockerfile through them. They build and start real containers, so they get a longer bound.
  go test -tags docker -count=1 -timeout 600s ./internal/environment/docker/... ./cmd/vibesys-cli
else
  printf 'No Docker daemon is reachable: the tests against a real one were compiled and not run.\n' >&2
fi

# More seeds without the detector, since a race the detector finds needs a schedule the first few
# hundred seeds may not reach. The count goes past seed 1519, which was the first to lose a cancel
# across recovery, and the run takes about 30 seconds.
VIBESYS_SIM_SEEDS=1530 go test -count=1 -timeout 300s -run TestSeedsHoldTheInvariants ./internal/sim
