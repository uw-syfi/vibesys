#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?usage: gateway/build.sh OUTPUT_DIRECTORY}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
cd "$(dirname "${BASH_SOURCE[0]}")"

for target in linux/amd64 linux/arm64 darwin/arm64; do
  target_os="${target%/*}"
  target_arch="${target#*/}"
  mkdir -p "$output_dir/$target_os-$target_arch"
  CGO_ENABLED=0 GOOS="$target_os" GOARCH="$target_arch" \
    go build -trimpath -ldflags='-s -w' \
      -o "$output_dir/$target_os-$target_arch/vibesysd" ./cmd/vibesysd
done
