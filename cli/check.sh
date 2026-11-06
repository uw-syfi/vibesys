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
# The kind tag adds the test of the Kubernetes leases against a real API server, which runs only
# where a cluster is named (see internal/lease/kubelease); it is vetted here so it always compiles.
go vet -tags kind ./internal/lease/kubelease/

# Compile every package's tests for each release target, so a symbol missing on
# another OS fails here and not only in CI. This runs before the tests because the
# release build test builds all four targets, and finds their standard library already
# compiled, but only if the flags match: build_release.sh uses -trimpath, and a cross target
# is built without cgo already, so the vet below uses -trimpath too. Without it the release build
# compiles the standard library again for each of the three targets, which costs about 35 seconds
# on a 4-core host with a cold cache.
# The host target was vetted above, with the same compiled packages.
host="$(go env GOOS)/$(go env GOARCH)"
for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  [[ "$target" == "$host" ]] && continue
  GOOS="${target%/*}" GOARCH="${target#*/}" GOFLAGS="${GOFLAGS:+$GOFLAGS }-trimpath" go vet -tags docker ./...
done

# Every test runs in at least one of two steps, and the tests against a real Docker daemon run beside
# the first.
#
# The tests that drive a real Docker daemon pass the same contracts the fakes pass, which is what
# lets the fakes stand in for Docker in every other test, and the tool as shipped runs a task of a
# Dockerfile through them. They build and start real containers, so they get a longer bound, and they
# spend their time waiting for the daemon, so they run while the race run keeps the cores busy and
# not after it. They run where a daemon is reachable, and are only compiled elsewhere. They build
# without cgo, as a release is: a task of two images mounts the tool itself in a container as the
# probe of the candidate's server, which only a static build can run whatever the image holds.
docker_log=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker_log="$(mktemp)"
  trap 'rm -f "$docker_log"' EXIT
  CGO_ENABLED=0 go test -tags docker -count=1 -timeout 600s ./internal/environment/docker/... ./cmd/vibesys-cli >"$docker_log" 2>&1 &
  docker_pid=$!
else
  printf 'No Docker daemon is reachable: the tests against a real one were compiled and not run.\n' >&2
fi

# The first step runs every package under the race detector, since the daemon's flows are concurrent
# and a package added later is covered without being listed here. The race run includes the tests of
# the release build and install.sh, which run real tools. The per-package timeout only guards against
# a hang. It does not build the sweeps: tests that walk a large space of cases on one goroutine, such
# as the exhaustive model check of the lifecycle machine and the crash of a store at every step of its
# script. With nothing running at once the detector checks nothing in them, and it makes them ten
# times slower. A sweep is a file named *_sweep_test.go that carries //go:build !race, and archcheck
# refuses one that starts a goroutine, so a test that needs the detector cannot be moved out of it by
# accident.
race_status=0
go test -race -count=1 -timeout 300s ./... || race_status=$?

if [[ -n "$docker_log" ]]; then
  docker_status=0
  wait "$docker_pid" || docker_status=$?
  cat "$docker_log"
  if [[ "$docker_status" -ne 0 ]]; then
    printf 'The tests against a real Docker daemon failed.\n' >&2
    exit "$docker_status"
  fi
fi
if [[ "$race_status" -ne 0 ]]; then
  exit "$race_status"
fi

# The second step runs every test again without the detector, which is how the binary ships, and it
# is where the sweeps run. It also widens the simulation to more seeds, since a race the detector
# finds needs a schedule the first few hundred seeds may not reach, and the seeds themselves need no
# detector. The count goes past seed 1519, which was the first to lose a cancel across recovery.
VIBESYS_SIM_SEEDS=1530 go test -count=1 -timeout 600s ./...
