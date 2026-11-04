#!/usr/bin/env bash
# Real Git and Buf are required: simulated revisions cannot expose this bug.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# Keep the fixture independent of user Git settings, identity and wall time.
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid
export GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z'
export UV_OFFLINE=true
export PROTO_BASE_REF=base

git init -q -b base "$work/repo"
cd "$work/repo"
cp "$root/buf.yaml" .
for layer in store search core cli; do
  mkdir -p "proto/vibesys/$layer/v1"
  cat > "proto/vibesys/$layer/v1/record.proto" <<PROTO
syntax = "proto3";
package vibesys.$layer.v1;
message Record { string original = 1; }
PROTO
done
git add .
git commit -qm baseline
git branch pr
# Main advances after the PR branches, adding both a file and a field.
cat > proto/vibesys/store/v1/new.proto <<'PROTO'
syntax = "proto3";
package vibesys.store.v1;
message NewRecord { string value = 1; }
PROTO
cat > proto/vibesys/search/v1/record.proto <<'PROTO'
syntax = "proto3";
package vibesys.search.v1;
message Record { string original = 1; string upstream = 2; }
PROTO
git add .
git commit -qm 'advance base'
git switch -q pr
cat > proto/vibesys/core/v1/additive.proto <<'PROTO'
syntax = "proto3";
package vibesys.core.v1;
message Additive { string value = 1; }
PROTO
git add .
git commit -qm 'additive PR'
"$root/scripts/check_proto_breaking.sh"
echo 'proto breaking: behind-base additive PR passes'

# Exercise both deletion and rename in every checked schema layer.
for layer in store search core cli; do
  for change in delete rename; do
    if [[ "$change" == delete ]]; then
      field=''
      expected='was deleted'
    else
      field='string renamed = 1;'
      expected='changed name'
    fi
    cat > "proto/vibesys/$layer/v1/record.proto" <<PROTO
syntax = "proto3";
package vibesys.$layer.v1;
message Record { $field }
PROTO
    if "$root/scripts/check_proto_breaking.sh" > "$work/output" 2>&1; then
      echo "Expected $layer field $change to fail" >&2
      exit 1
    fi
    if ! grep -q "$expected" "$work/output"; then
      cat "$work/output" >&2
      exit 1
    fi
    git restore "proto/vibesys/$layer/v1/record.proto"
  done
done
echo 'proto breaking: field deletions and renames fail in every checked layer'

# A local shallow clone has both tips but cannot reach their common ancestor.
git clone -q --no-local --depth=1 --branch pr "file://$work/repo" "$work/shallow"
cd "$work/shallow"
git fetch -q --depth=1 origin base:base
if "$root/scripts/check_proto_breaking.sh" > "$work/output" 2>&1; then
  echo 'Expected missing merge base to fail' >&2
  exit 1
fi
grep -q 'Cannot find the merge base of HEAD and base' "$work/output"
grep -q 'git fetch --unshallow' "$work/output"
echo 'proto breaking: shallow history fails with fetch guidance'

git fetch -q --unshallow origin
"$root/scripts/check_proto_breaking.sh"
echo 'proto breaking: full history restores the check'

# Keep historical blobs absent so the check must fetch them through the promisor.
cat > "$work/repo/proto/vibesys/core/v1/record.proto" <<'PROTO'
syntax = "proto3";
package vibesys.core.v1;
message Record { string original = 1; string added = 2; }
PROTO
git -C "$work/repo" add .
git -C "$work/repo" commit -qm 'change head blob without breaking compatibility'
git -C "$work/repo" config uploadpack.allowFilter true
git -C "$work/repo" config uploadpack.allowAnySHA1InWant true
git clone -q --filter=blob:none --branch pr "file://$work/repo" "$work/partial"
cd "$work/partial"
git branch base origin/base
missing_before="$(git rev-list --objects --all --missing=print | grep '^?' || true)"
baseline_blob="$(git rev-parse "$(git merge-base HEAD base):proto/vibesys/core/v1/record.proto")"
if ! grep -q "^?$baseline_blob$" <<< "$missing_before"; then
  echo 'Expected historical blobs to be absent in the partial clone' >&2
  exit 1
fi
"$root/scripts/check_proto_breaking.sh"
echo 'proto breaking: partial clone lazily fetches the merge-base schemas'
