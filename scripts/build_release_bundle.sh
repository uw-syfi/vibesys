#!/usr/bin/env bash
# Build the managed-run bundle for one platform:
#
#   build_release_bundle.sh VERSION OS ARCH OUTDIR
#
# VERSION is MAJOR.MINOR.PATCH with an optional -suffix and no leading "v"; OS is linux or darwin;
# ARCH is amd64 or arm64. OUTDIR gets vibesys_<version>_<os>_<arch>.tar.gz and its .sha256. The
# archive holds exactly what install.sh puts under ~/.vibesys/releases/<version>/:
#
#   bin/{vibesys-client,vibesys-host,vibesys-cli}   static Go binaries (CGO off)
#   sidecar/                                        pyproject.toml uv.lock README.md src/
#   marketplace/                                    .claude-plugin/marketplace.json and skills/
#   VERSION
#
# The sidecar's virtual environment is not in the archive: install.sh creates it with
# `uv sync --frozen`, because it must match the machine's Python.
#
# VIBESYS_CLIENT_SOURCE_DIR, for a test only, names a directory with its own go.mod and a main
# package to build as vibesys-client in place of gateway/cmd/vibesys-client.
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
case "$os" in
  linux | darwin) ;;
  *)
    echo "build_release_bundle.sh: OS must be linux or darwin, got '$os'" >&2
    exit 2
    ;;
esac
case "$arch" in
  amd64 | arm64) ;;
  *)
    echo "build_release_bundle.sh: ARCH must be amd64 or arm64, got '$arch'" >&2
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

# gobuild MODULE_DIR PACKAGE BINARY: one static binary for the target, stamped with the version the
# way cli/build_release.sh does. -buildvcs=false and -trimpath keep it independent of the checkout.
gobuild() {
  (cd "$1" && CGO_ENABLED=0 GOOS="$os" GOARCH="$arch" \
    go build -trimpath -buildvcs=false -ldflags "-s -w -X main.version=$version" \
    -o "$stage/bin/$3" "$2")
}
if [[ -n "${VIBESYS_CLIENT_SOURCE_DIR:-}" ]]; then
  gobuild "$VIBESYS_CLIENT_SOURCE_DIR" . vibesys-client
else
  gobuild "$root/gateway" ./cmd/vibesys-client vibesys-client
fi
gobuild "$root/gateway" ./cmd/vibesys-host vibesys-host
gobuild "$root/cli" ./cmd/vibesys-cli vibesys-cli

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
