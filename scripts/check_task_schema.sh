#!/usr/bin/env bash
# Hold the task manifest schemas in proto/vibesys/cli/v1 to their versioning rules against the merge
# base, beside buf breaking: scripts/check_task_schema.py says what the rules are.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_ref="${PROTO_BASE_REF:-origin/main}"
cd "$root"
if ! base="$(git merge-base HEAD "$base_ref")"; then
  echo "Cannot find the merge base of HEAD and $base_ref; fetch the base ref and full history (git fetch --unshallow in a shallow clone)." >&2
  exit 1
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
project="${VIBESYS_UV_PROJECT:-$root}"
buf() { uv run --project "$project" --locked --group proto buf "$@"; }
if git cat-file -e "$base:proto/vibesys/cli/v1" 2>/dev/null; then
  mkdir "$work/base"
  git archive --format=tar "$base" buf.yaml proto/ | tar -x -C "$work/base"
  buf build "$work/base" --exclude-source-info --path "$work/base/proto/vibesys/cli/v1" -o "$work/base.json#format=json"
else
  echo '{}' > "$work/base.json"
fi
buf build . --exclude-source-info --path proto/vibesys/cli/v1 -o "$work/head.json#format=json"
uv run --project "$project" --locked python -m scripts.check_task_schema "$work/base.json" "$work/head.json"
