#!/usr/bin/env bash
# The checks test_install.sh runs inside the clean container, as an ordinary user. Expects
# /opt/install.sh, the bundles of versions 1.0.0 and 1.0.1 in /bundles, and $ARCH.
set -euo pipefail

home="$HOME/.vibesys"
bin="$HOME/.local/bin"
v1="/bundles/vibesys_1.0.0_linux_${ARCH}.tar.gz"
v2="/bundles/vibesys_1.0.1_linux_${ARCH}.tar.gz"

check() {
  echo "check: $*"
}
die() {
  echo "FAIL: $*" >&2
  exit 1
}
install() {
  VIBESYS_INSTALL_BUNDLE="$1" sh /opt/install.sh "${@:2}"
}

# Nothing of the toolchain is here.
for tool in go python3 uv; do
  ! command -v "$tool" >/dev/null 2>&1 || die "$tool should not be installed in the clean container"
done

check "first install"
install "$v1" >/tmp/install1.log 2>&1 || {
  cat /tmp/install1.log
  die "install failed"
}
cat /tmp/install1.log

check "layout"
for f in bin/vibesys-client bin/vibesys-host bin/vibesys-cli sidecar/pyproject.toml sidecar/uv.lock \
  sidecar/README.md sidecar/src/vs_sidecar/__init__.py sidecar/.venv/bin/python \
  marketplace/.claude-plugin/marketplace.json marketplace/skills/README.md VERSION; do
  [ -e "$home/releases/1.0.0/$f" ] || die "missing $home/releases/1.0.0/$f"
done
[ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current does not point at releases/1.0.0"
[ "$(readlink "$bin/vibesys-client")" = "$home/current/bin/vibesys-client" ] || die "vibesys-client link is wrong"

check "only vibesys-client is on PATH"
export PATH="$bin:$PATH"
command -v vibesys-client >/dev/null || die "vibesys-client is not on PATH"
! command -v vibesys-host >/dev/null 2>&1 || die "vibesys-host must not be on PATH"
! command -v vibesys-cli >/dev/null 2>&1 || die "vibesys-cli must not be on PATH"
vibesys-client --version || vibesys-client --help >/dev/null

check "the sidecar imports"
"$home/current/sidecar/.venv/bin/python" -c 'import vs_sidecar'
check "the host's relative paths exist from its own binary"
(cd "$home/current/bin" && [ -x ../sidecar/.venv/bin/python ] && [ -x ./vibesys-cli ] && [ -d ../marketplace/skills ])

check "the install warned about PATH and confinement and gave the next steps"
grep -q "unconfined" /tmp/install1.log || die "no warning about unconfined agents"
grep -q "not on your PATH" /tmp/install1.log || die "no PATH hint"
grep -q "vibesys-client run start" /tmp/install1.log || die "no next steps"

check "re-install of the same version is a no-op"
before="$(stat -c '%i %Y' "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
install "$v1" >/tmp/install2.log 2>&1 || {
  cat /tmp/install2.log
  die "re-install failed"
}
after="$(stat -c '%i %Y' "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
[ "$before" = "$after" ] || die "re-install changed the release"
grep -q "already unpacked" /tmp/install2.log || die "re-install did not report the existing release"

check "a corrupted bundle leaves the previous install in place"
head -c 20000 "$v2" >/tmp/truncated_1.0.1.tar.gz
if install /tmp/truncated_1.0.1.tar.gz >/tmp/install3.log 2>&1; then die "truncated bundle was accepted"; fi
grep -q "failed while" /tmp/install3.log || die "failure did not name a step"
cp "$v2" /tmp/flipped.tar.gz
cp "$v2.sha256" /tmp/flipped.tar.gz.sha256
printf 'x' | dd of=/tmp/flipped.tar.gz bs=1 seek=100 conv=notrunc 2>/dev/null
if install /tmp/flipped.tar.gz >/tmp/install4.log 2>&1; then die "bundle with a wrong checksum was accepted"; fi
grep -q "checksum" /tmp/install4.log || die "wrong checksum was not reported as such"
[ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved after a failed install"
[ ! -e "$home/releases/1.0.1" ] || die "a failed install left a release behind"
ls -A "$home/releases" | grep -q '^\.stage' && die "a failed install left a staging directory"
vibesys-client --version || vibesys-client --help >/dev/null

check "a running host of the old version blocks a switch, and --force overrides"
(exec -a "$home/current/bin/vibesys-host" sleep 600) &
host_pid=$!
trap 'kill "$host_pid" 2>/dev/null || true' EXIT
if install "$v2" >/tmp/install5.log 2>&1; then die "switch was allowed while a host runs"; fi
grep -q "vibesys-host is running" /tmp/install5.log || die "refusal did not name the running host"
[ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved despite the refusal"
install "$v2" --force >/tmp/install6.log 2>&1 || {
  cat /tmp/install6.log
  die "--force install failed"
}
[ "$(readlink "$home/current")" = "releases/1.0.1" ] || die "--force did not switch"
[ -d "$home/releases/1.0.0" ] || die "the old release was removed"
"$home/current/sidecar/.venv/bin/python" -c 'import vs_sidecar'

echo "all install checks passed"
