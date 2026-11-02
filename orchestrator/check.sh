#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

unformatted="$(gofmt -l .)"
if [[ -n "$unformatted" ]]; then
  printf '%s\n' "$unformatted" >&2
  printf 'Run gofmt -w on the files above, then rerun orchestrator_checks.\n' >&2
  exit 1
fi

go vet ./...

# The core is pure and single-threaded, so only the store, which serves readers beside its writer,
# runs under the race detector.
go test -count=1 -timeout 180s ./...
go test -race -count=1 -timeout 180s -run 'PassesTheContract/ReadersAndClaimers' ./internal/store/
go test -race -count=1 -timeout 180s -run 'TestCommitsAndCompactionsAndVerifiesInterleave|TestVerifyRunsBesideTheWriter' ./internal/store/
