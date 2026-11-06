#!/usr/bin/env bash
# End to end test of install.sh on a clean machine: builds a bundle for this machine's
# architecture, then installs it in a fresh Debian container that has curl and nothing else (no Go,
# no Python, no uv, no repository), as an ordinary user. Needs Docker and the network (the
# container fetches uv, Python 3.12 and the sidecar's locked dependencies, as a user's machine does).
#
# The container checks are in test_install_in_container.sh, which this script mounts.
#
# vibesys-client is built from gateway/cmd/vibesys-client. While that package is not on this branch,
# the test builds a stub in its place that answers --version and --help, and says so. The stub
# exists only here; scripts/build_release_bundle.sh and install.sh know nothing of it.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${VIBESYS_INSTALL_TEST_IMAGE:-debian:bookworm-slim}"

case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *)
    echo "test_install.sh: unsupported architecture $(uname -m)" >&2
    exit 2
    ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 755 "$work"

stub_env=()
if [[ ! -d "$root/gateway/cmd/vibesys-client" ]]; then
  echo "test_install.sh: gateway/cmd/vibesys-client is absent; building a STUB vibesys-client for this test only" >&2
  mkdir -p "$work/stub"
  cat >"$work/stub/go.mod" <<'EOF'
module stub

go 1.21
EOF
  cat >"$work/stub/main.go" <<'EOF'
package main

import "fmt"

var version = "dev"

func main() { fmt.Println("vibesys-client (test stub)", version) }
EOF
  stub_env=(VIBESYS_CLIENT_SOURCE_DIR="$work/stub")
fi

mkdir -p "$work/bundles"
env "${stub_env[@]}" "$root/scripts/build_release_bundle.sh" 1.0.0 linux "$arch" "$work/bundles" >/dev/null

# A second version, made by repacking the first with another VERSION file: the install must be able
# to move from one release to the next.
mkdir -p "$work/repack"
tar -C "$work/repack" -xzf "$work/bundles/vibesys_1.0.0_linux_${arch}.tar.gz"
printf '1.0.1\n' >"$work/repack/VERSION"
tar -C "$work/repack" -czf "$work/bundles/vibesys_1.0.1_linux_${arch}.tar.gz" .
(cd "$work/bundles" && sha256sum "vibesys_1.0.1_linux_${arch}.tar.gz" >"vibesys_1.0.1_linux_${arch}.tar.gz.sha256")
rm -rf "$work/repack"

# Make everything readable by the unprivileged user in the container.
chmod -R a+rX "$work"

docker run --rm \
  -v "$root/install.sh:/opt/install.sh:ro" \
  -v "$root/scripts/tests/test_install_in_container.sh:/opt/checks.sh:ro" \
  -v "$work/bundles:/bundles:ro" \
  -e "ARCH=$arch" \
  "$image" bash -euc '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends curl ca-certificates procps git >/dev/null
    useradd -m tester
    runuser -u tester -- bash /opt/checks.sh
  '
echo "test_install.sh: ok"
