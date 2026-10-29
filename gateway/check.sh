#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

unformatted="$(gofmt -l .)"
if [[ -n "$unformatted" ]]; then
  printf '%s\n' "$unformatted" >&2
  printf 'Run gofmt -w on the files above, then rerun gateway_checks.\n' >&2
  exit 1
fi

go vet ./...
go test -count=1 -timeout 45s ./...

# Compile every package's tests for each release target, so a symbol missing on
# another OS fails here and not only in CI.
# The host target was vetted above, with the same compiled packages.
host="$(go env GOOS)/$(go env GOARCH)"
for target in linux/amd64 linux/arm64 darwin/arm64; do
  [[ "$target" == "$host" ]] && continue
  GOOS="${target%/*}" GOARCH="${target#*/}" go vet ./...
done
