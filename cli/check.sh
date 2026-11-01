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
go test -count=1 -timeout 45s ./...

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
  GOOS="${target%/*}" GOARCH="${target#*/}" go vet ./...
done
