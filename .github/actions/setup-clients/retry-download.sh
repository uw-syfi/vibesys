#!/usr/bin/env bash
# Retry one download command. Three attempts, then the command's own status.
# The wait stays here so a job timeout does not have to grow.
set -euo pipefail

attempts=3
delay=5

if [[ $# -lt 1 ]]; then
  echo "usage: retry-download.sh command [argument ...]" >&2
  exit 2
fi

attempt=1
while true; do
  set +e
  "$@"
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    exit 0
  fi
  if [[ "$attempt" -ge "$attempts" ]]; then
    exit "$status"
  fi
  echo "attempt ${attempt}/${attempts} failed (exit ${status}); waiting ${delay}s" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
  delay=$((delay * 2))
done
