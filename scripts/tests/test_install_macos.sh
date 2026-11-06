#!/usr/bin/env bash
# End to end test of the installed product on an Apple Silicon Mac: the macOS counterpart of
# test_install.sh. It builds the bundles of two versions for darwin/arm64, installs them with
# install.sh into a temporary HOME, and runs the phases of test_install_in_container.sh against that
# install, so the layout, the managed run with scripted agents and the verbs a person uses are one
# set of checks on both systems. No model is called.
#
# There is no container: the Mac is the machine. What makes it a clean one is the environment each
# phase gets. `env -i` leaves a HOME of its own, the system's PATH (no Go, uv or Python from the
# developer's tools) and nothing else, so the installer fetches uv and Python as a user's machine
# does, and the release finds its own parts with no setting. The client phase adds the TMPDIR a
# login shell has, under /var/folders, because that path is long and the sockets must not depend on it.
#
# Needs a Mac with Apple Silicon, git, curl and the network.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "test_install_macos.sh: needs an Apple Silicon Mac, this is $(uname -s) $(uname -m)" >&2
  exit 2
fi

# The work directory is short on purpose: the state directory under HOME holds sockets, and a Unix
# socket address holds 104 bytes on macOS. The long case is checked by the "macos" phase.
work="$(mktemp -d /tmp/vsmac.XXXXXX)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bundles" "$work/home" "$work/scratch"
"$root/scripts/build_release_bundle.sh" 1.0.0 darwin arm64 "$work/bundles" >/dev/null
"$root/scripts/build_release_bundle.sh" 1.0.1 darwin arm64 "$work/bundles" >/dev/null

mkdir -p "$work/scripted"
cp -R "$root/orchestrator/sidecar/e2e" "$work/scripted/e2e"
find "$work/scripted" -name __pycache__ -prune -exec rm -rf {} +

user_tmp="$(getconf DARWIN_USER_TEMP_DIR)"

# phase NAME [VAR=VALUE...]: run one phase of the checks as /bin/bash (the bash 3.2 a Mac has) in an
# environment of only what is listed here.
phase() {
  local name="$1"
  shift
  env -i HOME="$work/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    ARCH=arm64 OS=darwin CLEAN_MACHINE=0 SCRATCH="$work/scratch" \
    INSTALL_SH="$root/install.sh" BUNDLES_DIR="$work/bundles" SCRIPTED_DIR="$work/scripted" \
    "$@" /bin/bash "$root/scripts/tests/test_install_in_container.sh" "$name"
}

phase install
phase product
phase client TMPDIR="$user_tmp"
phase macos TMPDIR="$user_tmp"
echo "test_install_macos.sh: ok"
