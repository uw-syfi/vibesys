#!/usr/bin/env bash
# Rewrite the committed goldens that a converter or gateway change invalidates, then print what
# changed. Commit all of it. This is the one command the stale-golden failures name.
#
#   conversion      proto/testdata/conversion (Python converter and writer)
#   skeleton wire   proto/testdata/skeleton-wire and the retained gateway replies (Go gateway)
#   queue-rs-live   the committed copy of the desktop journey trace, queue-rs-live.json
#
# Not rewritten, because nothing is committed: the native replays the desktop e2e and the run page
# tests read. They are generated from their declarations under the hash of their inputs (see
# tests/support/derived_replays.py), so a projector or `.proto` change never leaves one stale.
# Not rewritten either: the UI gallery screenshot baselines. They need Docker and are rendered from
# the UI, not from the schema; see "UI gallery" in docs/contributing/clients-architecture.md.
#
# Order follows the data: the conversion goldens read only sources, and the gateway test reads the
# conversion goldens. The first failure stops the rest.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
goldens=(clients/libs/run-fixtures/src/data/queue-rs-live.json proto/testdata/conversion
  proto/testdata/skeleton-wire proto/testdata/retained)
started=$SECONDS
echo "regenerate_goldens: queue-rs-live and the conversion goldens"
uv run --locked python -m tests.support.shared_scenarios
uv run --locked python proto/testdata/conversion/generate.py
echo "regenerate_goldens: skeleton wire and retained gateway replies"
(cd gateway && VIBESYS_UPDATE_GOLDEN=1 go test -count=1 ./internal/server/...)
echo "regenerate_goldens: rewrote these (git status) in $((SECONDS - started)) s:"
git status --short --untracked-files=all -- "${goldens[@]}"
echo "regenerate_goldens: not regenerated: UI gallery screenshot baselines (need Docker; see docs/contributing/clients-architecture.md)"
