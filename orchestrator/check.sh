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

# The plain run is bound by its slowest package (the core's simulation, one thread), so the race
# runs below use the idle cores beside it instead of after it: the check has to fit its budget in
# .repoctl/checks.toml, and it stood at ninety-seven percent of it with the two one after the other.
go test -count=1 -timeout 180s ./... &
plain=$!

# The core is pure and single-threaded. The store serves readers beside its writer, and the other
# packages hold the goroutines (streams, pipes, the serving loop), so they run again under the race
# detector.
go test -race -count=1 -timeout 180s -run 'PassesTheContract/ReadersAndClaimers' ./internal/store/
go test -race -count=1 -timeout 180s -run 'TestCommitsAndCompactionsAndVerifiesInterleave|TestVerifyRunsBesideTheWriter' ./internal/store/
go test -race -count=1 -timeout 300s ./internal/hub ./internal/daemon/... ./internal/client ./internal/mcp ./internal/production ./internal/search

wait "$plain"
