#!/usr/bin/env bash
# Rewrite every golden that a records projector or `.proto` change invalidates, then print what
# changed. Commit all of it. This is the one command the stale-golden failures name.
#
#   native replays    clients/libs/run-fixtures/src/native, clients/apps/desktop-shell/e2e/scenarios
#   conversion        proto/testdata/conversion
#   skeleton wire     proto/testdata/skeleton-wire (and the gateway's retained RPC replies)
#
# Not rewritten: the UI gallery screenshot baselines. They need Docker and are rendered from the
# UI, not from the schema; see "UI gallery" in docs/contributing/clients-architecture.md.
#
# Order follows the data: the native replays and the conversion goldens read only sources, the
# gateway test reads the conversion goldens, and the input hashes are recorded last. The three
# generators beside each other share no file, so they run at once; the projection of the two
# 600 idea fixtures takes minutes and is most of the time. The first failure stops the rest.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
goldens=(clients/libs/run-fixtures/src/native clients/libs/run-fixtures/src/data/queue-rs-live.json
  clients/apps/desktop-shell/e2e/scenarios proto/testdata/conversion proto/testdata/skeleton-wire
  proto/testdata/retained)
logs="$(mktemp -d)"
pids=()
stop_all() {
  local pid
  for pid in ${pids[@]+"${pids[@]}"}; do
    [[ -z "$pid" ]] || kill "$pid" 2>/dev/null || true
  done
}
trap 'stop_all; rm -rf "$logs"' EXIT

# Run "name|command" generators that share no file at once. Polling ends the others at the first
# failure, whichever generator it is.
run_together() {
  local names=() index name status spec running=0
  pids=()
  for spec in "$@"; do
    name="${spec%%|*}"
    names+=("$name")
    bash -c "${spec#*|}" >"$logs/$name.log" 2>&1 &
    pids+=("$!")
    running=$((running + 1))
  done
  while ((running > 0)); do
    for index in "${!pids[@]}"; do
      [[ -n "${pids[index]}" ]] || continue
      ! kill -0 "${pids[index]}" 2>/dev/null || continue
      status=0
      wait "${pids[index]}" || status=$?
      name="${names[index]}"
      if ((status != 0)); then
        echo "regenerate_goldens: $name failed (exit $status); its last lines:" >&2
        tail -n 40 "$logs/$name.log" >&2
        return "$status"
      fi
      echo "regenerate_goldens: $name done"
      pids[index]=""
      running=$((running - 1))
    done
    sleep 1
  done
}

started=$SECONDS
echo "regenerate_goldens: native replays and conversion goldens, together"
run_together \
  'native-shared|uv run --locked python -m tests.support.shared_scenarios' \
  'native-journeys|uv run --locked python -m tests.support.journey_fixtures' \
  'conversion|uv run --locked python proto/testdata/conversion/generate.py'
pids=()
echo "regenerate_goldens: skeleton wire and retained gateway replies"
(cd gateway && VIBESYS_UPDATE_GOLDEN=1 go test -count=1 ./internal/server/...)
echo "regenerate_goldens: recording the hash of each golden's inputs"
uv run --locked python -m tests.support.wire_goldens
echo "regenerate_goldens: rewrote these (git status) in $((SECONDS - started)) s:"
git status --short --untracked-files=all -- "${goldens[@]}"
echo "regenerate_goldens: not regenerated: UI gallery screenshot baselines (need Docker; see docs/contributing/clients-architecture.md)"
