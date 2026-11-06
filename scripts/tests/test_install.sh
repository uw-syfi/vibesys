#!/usr/bin/env bash
# End to end test of the installed product on a clean machine: builds real bundles of two versions
# for this machine's architecture, then, in a fresh Debian container as an ordinary user, installs
# them and runs a managed run with what the bundle installed. Needs Docker and the network (the
# container fetches uv, Python 3.12 and the sidecar's locked dependencies, as a user's machine does).
#
# The container starts with curl and nothing else: no Go, no Python, no uv, no git, no repository.
# Two phases run there, both in test_install_in_container.sh, which this script mounts:
#
#   install  the installer and the layout it makes, with no git in the image;
#   product  git is added (a task lives in a git repository), then a run is started with the
#            installed vibesys-client and no setting, and again with scripted agents.
#
# The scripted agents are orchestrator/sidecar/e2e, copied into the container as test code. They
# replace only the session holder, named by `vibesys-client host configure`; the host, client,
# vibesys-cli and marketplace are the bundle's own. No model is called.
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

# Two versions, so the install can move from one release to the next and the stamped version can
# be told apart from a default.
mkdir -p "$work/bundles"
"$root/scripts/build_release_bundle.sh" 1.0.0 linux "$arch" "$work/bundles" >/dev/null
"$root/scripts/build_release_bundle.sh" 1.0.1 linux "$arch" "$work/bundles" >/dev/null

# The scripted agents' code, which is not part of any bundle.
mkdir -p "$work/scripted"
cp -R "$root/orchestrator/sidecar/e2e" "$work/scripted/e2e"
find "$work/scripted" -name __pycache__ -prune -exec rm -rf {} +

# Make everything readable by the unprivileged user in the container.
chmod -R a+rX "$work"

docker run --rm \
  -v "$root/install.sh:/opt/install.sh:ro" \
  -v "$root/scripts/tests/test_install_in_container.sh:/opt/checks.sh:ro" \
  -v "$work/bundles:/bundles:ro" \
  -v "$work/scripted:/opt/scripted:ro" \
  -e "ARCH=$arch" \
  "$image" bash -euc '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends curl ca-certificates procps >/dev/null
    useradd -m tester
    runuser -u tester -- bash /opt/checks.sh install
    apt-get install -y -qq --no-install-recommends git >/dev/null
    runuser -u tester -- bash /opt/checks.sh product
  '
echo "test_install.sh: ok"
