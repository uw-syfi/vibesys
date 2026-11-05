#!/usr/bin/env bash
# The task schema check on fixture schemas, with real Git and Buf: a schema of major version 0 is
# frozen, and one of 1.0 on grows only by fields that declare a raised minor version, with its
# protovalidate rules unchanged. A rename is Buf's to refuse, beside it.
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
# The fixture repository has no Python project of its own: the checks run in this one's.
export VIBESYS_UV_PROJECT="$root"

repo="$work/repo"
git init -q -b base "$repo"
cp "$root/buf.yaml" "$repo/"
mkdir -p "$repo/proto/vibesys/cli/v1" "$repo/scripts"
cp -R "$root/proto/buf" "$repo/proto/"
# The checker finds the repository from its own path, so it runs from the fixture's copy.
cp "$root/scripts/check_task_schema.py" "$root/scripts/check_task_schema.sh" "$repo/scripts/"
touch "$repo/scripts/__init__.py"

# schema writes the fixture's task.proto: a 1.x schema whose fields are given, and a frozen 0.1 one.
schema() {
  local minor="$1" fields="$2" frozen="${3:-string version = 1 [(task_schema_since_minor) = 1];}"
  cat > "$repo/proto/vibesys/cli/v1/task.proto" <<PROTO
syntax = "proto3";
package vibesys.cli.v1;

import "buf/validate/validate.proto";
import "google/protobuf/descriptor.proto";

message TaskSchema {
  uint32 major = 1;
  uint32 minor = 2;
}

extend google.protobuf.MessageOptions {
  TaskSchema task_schema = 51001;
}

extend google.protobuf.FieldOptions {
  optional uint32 task_schema_since_minor = 51002;
}

message TaskManifestV01 {
  option (task_schema) = {major: 0 minor: 1};
  $frozen
}

message TaskManifestV1 {
  option (task_schema) = {major: 1 minor: $minor};
  $fields
}
PROTO
}

check() {
  (cd "$repo" && ./scripts/check_task_schema.sh) > "$work/output" 2>&1
}

pass() {
  if ! check; then
    cat "$work/output" >&2
    echo "Expected $1 to pass" >&2
    exit 1
  fi
  echo "task schema: $1 passes"
}

fails() {
  local name="$1" expected="$2"
  if check; then
    echo "Expected $name to fail" >&2
    exit 1
  fi
  if ! grep -q -- "$expected" "$work/output"; then
    cat "$work/output" >&2
    echo "Expected $name to say: $expected" >&2
    exit 1
  fi
  echo "task schema: $name fails"
}

base_fields='string version = 1 [(task_schema_since_minor) = 0];
  uint32 workers = 2 [(task_schema_since_minor) = 0, (buf.validate.field).uint32.lte = 64];'
schema 0 "$base_fields"
(cd "$repo" && git add -A && git commit -qm baseline && git switch -qc pr)

pass "an unchanged schema"

schema 0 "$base_fields
  string note = 3 [(task_schema_since_minor) = 0];"
fails "a field added without a minor version" "raise its minor version to 1"

schema 1 "$base_fields
  string note = 3 [(task_schema_since_minor) = 1];"
pass "a field added with the minor version raised"

schema 1 "$base_fields
  string note = 3;"
fails "a field that declares no minor version" "declares no task_schema_since_minor"

schema 2 "$base_fields
  string note = 3 [(task_schema_since_minor) = 1];"
fails "a field added under an older minor version" "not the schema's minor version 2"

schema 0 'string version = 1 [(task_schema_since_minor) = 0];
  uint32 workers = 2 [(task_schema_since_minor) = 0, (buf.validate.field).uint32.lte = 32];'
fails "a rule tightened" "tightening needs a new major; loosening needs a reviewed waiver"

schema 0 'string version = 1 [(task_schema_since_minor) = 0];
  uint32 workers = 2 [(task_schema_since_minor) = 0, (buf.validate.field).uint32.lte = 128];'
fails "a rule loosened" "tightening needs a new major; loosening needs a reviewed waiver"

schema 0 "$base_fields" 'string version = 1 [(task_schema_since_minor) = 1];
  string extra = 2 [(task_schema_since_minor) = 1];'
fails "a change to a 0.x schema" "TaskManifestV01: task schema 0.1 is frozen"

# A renamed field keeps its number, so the rules above see the same field; Buf refuses it.
schema 0 'string version = 1 [(task_schema_since_minor) = 0];
  uint32 parallelism = 2 [(task_schema_since_minor) = 0, (buf.validate.field).uint32.lte = 64];'
(cd "$repo" && git archive --format=tar base buf.yaml proto/ > "$work/base.tar")
if (cd "$repo" && uv run --project "$root" --locked --group proto buf breaking --path proto/vibesys/cli/v1 --against "$work/base.tar") > "$work/output" 2>&1; then
  echo "Expected buf breaking to refuse a renamed field" >&2
  exit 1
fi
grep -q 'changed name' "$work/output" || { cat "$work/output" >&2; exit 1; }
echo 'task schema: a renamed field fails buf breaking'
