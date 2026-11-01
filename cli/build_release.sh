#!/usr/bin/env bash
# Build the assets of one vibesys-cli release into an empty directory:
#
#   build_release.sh <version> <out-dir>
#
# <version> is MAJOR.MINOR.PATCH with an optional -suffix and no leading "v". The directory gets
# one static binary per release target, named vibesys-cli_<os>_<arch>, then checksums.txt (sha256sum
# format) and VERSION. The names carry no version, so the same files can be served from any tag.
# internal/upgrade names the same assets; its tests build a release with this script and check
# that they agree.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ $# -ne 2 ]]; then
  echo "usage: build_release.sh <version> <out-dir>" >&2
  exit 2
fi
version="$1"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "build_release.sh: version must be MAJOR.MINOR.PATCH[-suffix] without a leading v, got '$version'" >&2
  exit 2
fi
mkdir -p "$2"
out="$(cd "$2" && pwd)"
if [[ -n "$(ls -A "$out")" ]]; then
  echo "build_release.sh: $out is not empty" >&2
  exit 2
fi

for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
  # -buildvcs=false and -trimpath keep the binary independent of the checkout it was built in;
  # CGO_ENABLED=0 makes it static; main.version is what --version prints.
  CGO_ENABLED=0 GOOS="${target%/*}" GOARCH="${target#*/}" \
    go build -trimpath -buildvcs=false \
      -ldflags "-s -w -X main.version=$version" \
      -o "$out/vibesys-cli_${target%/*}_${target#*/}" ./cmd/vibesys-cli
done

cd "$out"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum vibesys-cli_* > checksums.txt
else
  shasum -a 256 vibesys-cli_* > checksums.txt
fi
printf '%s\n' "$version" > VERSION
