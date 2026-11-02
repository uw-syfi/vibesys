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

# The core is pure and single-threaded and nothing here starts a goroutine yet, so there is no race
# run. The first concurrent package adds one, as cli/check.sh does.
go test -count=1 -timeout 180s ./...
