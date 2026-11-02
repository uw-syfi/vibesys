#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

unformatted="$(gofmt -l .)"
if [[ -n "$unformatted" ]]; then
  printf '%s\n' "$unformatted" >&2
  printf 'Run gofmt -w on the files above, then rerun cli_checks.\n' >&2
  exit 1
fi

# The docker tag adds the tests that drive a real Docker daemon. They are vetted everywhere, so they
# always compile, and run below where a daemon is reachable.
go vet -tags docker ./...
go test -count=1 -timeout 45s ./...

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  # They pass the same contracts the fakes pass, which is what lets the fakes stand in for Docker
  # in every other test, and the tool as shipped runs a task of a Dockerfile through them. They
  # build and start real containers, so they get a longer bound.
  go test -tags docker -count=1 -timeout 600s ./internal/environment/docker/... ./cmd/vibesys-cli
else
  printf 'No Docker daemon is reachable: the tests against a real one were compiled and not run.\n' >&2
fi

# The concurrency tests prove nothing without the race detector, and the whole suite under it takes
# several times longer, so only they run with it. A new package's concurrency test belongs here by
# the name pattern: a contract suite's "concurrency" group, or a test whose name starts with Parallel.
go test -race -count=1 -timeout 120s -run '^(TestContract.*|TestParallel.*)$/^concurrency$' ./internal/store/...

# Compile every package's tests for each release target, so a symbol missing on
# another OS fails here and not only in CI.
# The host target was vetted above, with the same compiled packages.
host="$(go env GOOS)/$(go env GOARCH)"
for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  [[ "$target" == "$host" ]] && continue
  GOOS="${target%/*}" GOARCH="${target#*/}" go vet -tags docker ./...
done
