#!/usr/bin/env bash
# Check the current repository's schemas against their shared branch history.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base_ref="${PROTO_BASE_REF:-origin/main}"
if ! base="$(git merge-base HEAD "$base_ref")"; then
  echo "Cannot find the merge base of HEAD and $base_ref; fetch the base ref and full history (git fetch --unshallow in a shallow clone)." >&2
  exit 1
fi
# Only the first introduction has no store to compare.
if git cat-file -e "$base:buf.yaml" 2>/dev/null; then
  baseline="$(mktemp -d)"
  trap 'rm -rf "$baseline"' EXIT
  # Git resolves promised blobs through this clone's remote before Buf reads them.
  git archive --format=tar "$base" buf.yaml proto/ > "$baseline/base.tar"
  uv run --project "$root" --locked --group proto buf breaking --path proto/vibesys/store/v1 --path proto/vibesys/search/v1 --path proto/vibesys/core/v1 \
    --against "$baseline/base.tar"
elif git cat-file -e "$base:proto/vibesys/store/v1" 2>/dev/null; then
  echo "Store exists at $base but buf.yaml is missing; restore its breaking-check configuration." >&2
  exit 1
else
  echo "No store schema at $base; establishing the initial additive baseline."
fi
