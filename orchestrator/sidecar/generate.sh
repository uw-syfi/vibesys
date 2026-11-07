#!/usr/bin/env bash
# Regenerates the sidecar's Python bindings into src/vs_sidecar/gen from the orchestrator protos.
# They live in the sidecar's own tree, beside its own lock, so the repository's Python generation
# (buf.gen.yaml, which excludes this package) is untouched. Run from anywhere; needs the root
# `proto` dependency group for buf and this project's dev group for grpcio-tools.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
out="$here/src/vs_sidecar/gen"
image="$(mktemp -d)/image.binpb"
trap 'rm -rf "$(dirname "$image")"' EXIT

(cd "$root" && uv run --locked --group proto buf build proto --path proto/vibesys/orchestrator -o "$image")

rm -rf "$out"
mkdir -p "$out"
files="$(cd "$root" && uv run --locked --group proto buf ls-files proto --path proto/vibesys/orchestrator --format=import)"
(cd "$here" && uv run --locked --group dev python -m grpc_tools.protoc \
  --descriptor_set_in="$image" \
  --python_out="$out" --grpc_python_out="$out" \
  $files buf/validate/validate.proto)

# protoc writes absolute imports of the proto package paths; point them at this package so the
# generated tree does not shadow another `vibesys` or `buf` in the interpreter.
grep -rl --include='*.py' -E '^from (vibesys|buf)\.' "$out" | while read -r file; do
  # `-i.bak` with the suffix attached is the one in-place form GNU and BSD sed (macOS) share.
  sed -i.bak -E "s/^from (vibesys|buf)\./from vs_sidecar.gen.\1./; s/^import (vibesys|buf)\./import vs_sidecar.gen.\1./" "$file"
  rm -f "$file.bak"
done
find "$out" -type d -exec sh -c 'test -f "$1/__init__.py" || : > "$1/__init__.py"' _ {} \;
