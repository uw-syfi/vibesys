#!/usr/bin/env bash
# Build the managed-run bundle for one platform:
#
#   build_release_bundle.sh VERSION OS ARCH OUTDIR
#
# VERSION is MAJOR.MINOR.PATCH with an optional -suffix and no leading "v". The platforms are exactly
# linux/amd64, linux/arm64 and darwin/arm64. OUTDIR gets vibesys_<version>_<os>_<arch>.tar.gz and
# its .sha256. The archive holds exactly what install.sh puts under ~/.vibesys/releases/<version>/:
#
#   bin/{vibesys-client,vibesys-host,vibesys-cli}   static Go binaries (CGO off)
#   sidecar/                                        pyproject.toml uv.lock README.md src/
#   marketplace/                                    .claude-plugin/marketplace.json and skills/
#   VERSION
#
# The sidecar's virtual environment is not in the archive: install.sh creates it with
# `uv sync --frozen`, because it must match the machine's Python.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: build_release_bundle.sh VERSION OS ARCH OUTDIR" >&2
  exit 2
fi
version="$1" os="$2" arch="$3"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "build_release_bundle.sh: version must be MAJOR.MINOR.PATCH[-suffix] without a leading v, got '$version'" >&2
  exit 2
fi
case "$os/$arch" in
  linux/amd64 | linux/arm64 | darwin/arm64) ;;
  *)
    echo "build_release_bundle.sh: platform must be linux/amd64, linux/arm64 or darwin/arm64, got '$os/$arch'" >&2
    exit 2
    ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$4"
out="$(cd "$4" && pwd)"
name="vibesys_${version}_${os}_${arch}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/bin" "$stage/sidecar" "$stage/marketplace/.claude-plugin"

# gobuild MODULE_DIR PACKAGE BINARY VERSION_VAR: one static binary for the target. VERSION_VAR is the
# Go variable that holds the binary's version, which -X sets to this release's, so that the binary
# reports it. -buildvcs=false and -trimpath keep it independent of the checkout.
gobuild() {
  (cd "$1" && CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" \
    go build -trimpath -buildvcs=false -ldflags "-s -w -X $4=$version" \
    -o "$stage/bin/$3" "$2")
}
# The gateway's binaries share one version variable; vibesys-cli keeps its own, in its main package.
gateway_version="github.com/uw-syfi/vibesys/gateway/internal/buildinfo.Version"
gobuild "$root/gateway" ./cmd/vibesys-client vibesys-client "$gateway_version"
gobuild "$root/gateway" ./cmd/vibesys-host vibesys-host "$gateway_version"
gobuild "$root/cli" ./cmd/vibesys-cli vibesys-cli main.version

# The sidecar: the project file, its lock, the readme and the source, and nothing else. The
# generated bindings under src/ are tracked, so `uv sync --frozen` needs no build step.
side="$root/orchestrator/sidecar"
cp "$side/pyproject.toml" "$side/uv.lock" "$side/README.md" "$stage/sidecar/"
cp -R "$side/src" "$stage/sidecar/src"
find "$stage/sidecar" -name __pycache__ -prune -exec rm -rf {} +

# The marketplace the host hands to Claude Code: the manifest and the plugin sources it names.
cp "$root/.claude-plugin/marketplace.json" "$stage/marketplace/.claude-plugin/"
cp -R "$root/skills" "$stage/marketplace/skills"
find "$stage/marketplace" -name __pycache__ -prune -exec rm -rf {} +

printf '%s\n' "$version" > "$stage/VERSION"
tar -C "$stage" -czf "$out/$name.tar.gz" .
cd "$out"
if command -v sha256sum > /dev/null 2>&1; then
  sha256sum "$name.tar.gz" > "$name.tar.gz.sha256"
else
  shasum -a 256 "$name.tar.gz" > "$name.tar.gz.sha256"
fi
echo "$out/$name.tar.gz"
