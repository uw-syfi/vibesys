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

# The plain run does not use every core all the time, so the race runs below use the idle cores
# beside it instead of after it: the check has to fit its budget in .repoctl/checks.toml, and it
# stood at ninety-seven percent of it with the two one after the other.
go test -count=1 -timeout 180s ./... &
plain=$!

# The core is pure and single-threaded. The store serves readers beside its writer, and the other
# packages that start goroutines (streams, pipes, the serving loop) run again under the race
# detector; the others have nothing for it to find, which internal/archcheck holds. The seeded properties of those
# packages go through a quarter of their seeds under it (thinnedUnderRace in their tests): the plain run covers the input space, this one the interleavings.
go test -race -count=1 -timeout 180s -run 'PassesTheContract/ReadersAndClaimers|TestCommitsAndCompactionsAndVerifiesInterleave|TestVerifyRunsBesideTheWriter' ./internal/store/
go test -race -count=1 -timeout 300s ./internal/daemon/... ./internal/client ./internal/clidaemon ./internal/production ./privatesocket ./clientcli ./hosted

wait "$plain"
