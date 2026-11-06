#!/bin/sh
# Install VibeSys for managed runs from GitHub Releases:
#
#   curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/install.sh | sh
#
# It downloads the bundle for this operating system and architecture, checks it against the
# release's published SHA-256, unpacks it under ~/.vibesys/releases/<version>/, creates the
# sidecar's Python environment there with uv (installing uv into ~/.vibesys/uv first if you have
# none), and then points ~/.vibesys/current at it and ~/.local/bin/vibesys-client at that. It never
# uses root and never edits a shell profile. Only vibesys-client goes on your PATH; the other
# binaries are found by the host relative to itself.
#
# Options and settings, all optional:
#
#   --force                  switch versions without asking a running vibesys-host to stop, even with live runs
#   VIBESYS_VERSION          install this version (for example 0.2.0) instead of the newest
#   VIBESYS_INSTALL_DIR      put the vibesys-client link here instead of $HOME/.local/bin
#   VIBESYS_HOME             keep releases here instead of $HOME/.vibesys
#   VIBESYS_INSTALL_BUNDLE   install from this local vibesys_<version>_<os>_<arch>.tar.gz and never
#                            touch the network for the bundle (uv and Python are still fetched if
#                            missing). A .sha256 file beside it is checked when present.
#   VIBESYS_RELEASES_URL     where release assets are served, for a mirror or a test; the default is
#                            https://github.com/uw-syfi/vibesys/releases/download
#
# A failure at any step names the step and leaves the previous install working: the new release is
# unpacked beside the old ones, and `current` moves only after every step has passed. Running the
# same version again changes nothing. A switch to another version asks a running vibesys-host to
# stop, and is refused while it has live runs or does not answer, unless --force is given; see
# stop_host_for_switch.
#
# The whole script is a function called on the last line, so a download cut short does nothing.

step="starting"

fail() {
  printf 'install.sh: failed while %s: %s\nThe previous install, if any, is unchanged.\n' "$step" "$*" >&2
  exit 1
}

# fetch URL DEST: download one file or fail.
fetch() {
  try_fetch "$1" "$2" || fail "cannot download $1"
}

# try_fetch URL DEST: download one file, returning its status.
try_fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    fail "needs curl or wget"
  fi
}

# sha256_of FILE: print the file's SHA-256 as lowercase hex.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    fail "needs sha256sum or shasum to verify the download"
  fi
}

detect_os() {
  case "$(uname -s)" in
    Linux) echo linux ;;
    Darwin) echo darwin ;;
    *) fail "unsupported operating system '$(uname -s)'; releases cover Linux and macOS" ;;
  esac
}

# detect_arch OS: print amd64 or arm64. Releases cover linux/amd64, linux/arm64 and darwin/arm64.
detect_arch() {
  case "$(uname -m)" in
    x86_64 | amd64)
      if [ "$1" = darwin ]; then
        # A shell running under Rosetta reports x86_64 on an Apple Silicon Mac, which gets arm64.
        if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
          echo arm64
        else
          fail "VibeSys releases do not cover Intel Macs; they cover Apple Silicon Macs and Linux (amd64 and arm64)"
        fi
      else
        echo amd64
      fi
      ;;
    aarch64 | arm64) echo arm64 ;;
    *) fail "unsupported architecture '$(uname -m)'; releases cover amd64 and arm64 on Linux and arm64 on macOS" ;;
  esac
}

# verify_checksum FILE EXPECTED_LINE_FILE: the checksum file holds "<hex>  <name>" for FILE.
verify_checksum() {
  expected="$(awk 'NR == 1 { print $1 }' "$2")"
  case "$expected" in
    "" | *[!0-9a-f]*) fail "$2 does not hold a SHA-256" ;;
  esac
  [ "${#expected}" -eq 64 ] || fail "$2 does not hold a SHA-256"
  actual="$(sha256_of "$1")"
  [ "$actual" = "$expected" ] ||
    fail "$(basename "$1") does not match its published checksum (got $actual, expected $expected)"
}

# link_to TARGET LINK: make LINK a symlink to TARGET, replacing a symlink in one rename. GNU mv
# takes -T and BSD mv (macOS) takes -h, and both treat LINK as a name, not as a directory to enter.
link_to() {
  tmp_link="$(dirname "$2")/.$(basename "$2").$$"
  rm -f "$tmp_link"
  ln -s "$1" "$tmp_link" || fail "cannot create a link in $(dirname "$2")"
  if ! mv -fT "$tmp_link" "$2" 2>/dev/null && ! mv -fh "$tmp_link" "$2" 2>/dev/null; then
    rm -f "$tmp_link"
    ln -sfn "$1" "$2" || fail "cannot replace $2"
  fi
}

# release_ready DIR: DIR is a complete, installed release. The release says what that means: its
# own vibesys-client checks it against the release.json it holds.
release_ready() {
  [ -x "$1/bin/vibesys-client" ] && "$1/bin/vibesys-client" release verify --installed "$1" >/dev/null 2>&1
}

# unpack_bundle BUNDLE: extract the bundle into $stage, refusing names outside it.
unpack_bundle() {
  step="unpacking the bundle"
  # Reject absolute and parent-relative names before extracting anything.
  tar -tzf "$1" >"$work/names" 2>/dev/null || fail "$1 is not a readable VibeSys bundle"
  if grep -E '^/|(^|/)\.\.(/|$)' "$work/names" >/dev/null; then
    fail "the bundle contains paths outside its own directory"
  fi
  rm -rf "$stage"
  mkdir -p "$stage" || fail "cannot create $stage"
  tar -xzf "$1" -C "$stage" || fail "cannot extract the bundle"
}

# stage_version: check the unpacked bundle in $stage against its own manifest and print its version.
# The bundle's vibesys-client does the check, so running it also proves the binaries run here.
stage_version() {
  step="checking the bundle"
  [ -x "$stage/bin/vibesys-client" ] || fail "the bundle has no bin/vibesys-client to check it with"
  "$stage/bin/vibesys-client" release verify "$stage" || fail "the bundle is not a whole VibeSys release that runs on this machine"
}

# current_version: the version `current` points at, or nothing.
current_version() {
  if [ -L "$home/current" ]; then
    basename "$(readlink "$home/current")"
  fi
}

# stop_host_for_switch: before moving `current` to another version, ask the host that is running to
# end, through the new release's own client, which is the one that is certain to speak the question.
# `host stop --if-no-live-runs` answers in its exit code: 0 when no host is left (none was running,
# or an idle one stopped), 1 when the host has live runs and stays, 4 when a host did not answer
# (one that predates host control). Nothing here reads the process list.
stop_host_for_switch() {
  [ "$force" = 0 ] || return 0
  [ -n "$(current_version)" ] && [ "$(current_version)" != "$1" ] || return 0
  status=0
  "$2/bin/vibesys-client" --root "$home" host stop --if-no-live-runs || status=$?
  case "$status" in
    0) ;;
    1) fail "the running vibesys-host has live runs. Stop them, or run again with --force to switch anyway; agents already running keep using the old release, which is left in place" ;;
    *) fail "the running vibesys-host did not answer (exit $status). Stop it, or run again with --force to switch anyway" ;;
  esac
}

# find_uv: print the path of a usable uv, installing one into $home/uv if there is none.
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  if [ -x "$home/uv/uv" ]; then
    echo "$home/uv/uv"
    return
  fi
  printf 'uv (the Python package manager) is not installed. Installing it from astral.sh into %s/uv.\n' "$home" >&2
  uv_script="$work/uv-install.sh"
  fetch "https://astral.sh/uv/install.sh" "$uv_script"
  # UV_UNMANAGED_INSTALL puts the binaries in that directory and edits no shell profile.
  UV_UNMANAGED_INSTALL="$home/uv" UV_NO_MODIFY_PATH=1 sh "$uv_script" >&2 ||
    fail "the uv installer failed; install uv yourself (https://docs.astral.sh/uv/) and run this again"
  [ -x "$home/uv/uv" ] || fail "the uv installer did not produce $home/uv/uv"
  echo "$home/uv/uv"
}

main() {
  force=0
  for arg in "$@"; do
    case "$arg" in
      --force) force=1 ;;
      *) fail "unknown option '$arg'; the only option is --force" ;;
    esac
  done

  step="reading settings"
  [ -n "${HOME:-}" ] || fail "HOME is not set"
  home="${VIBESYS_HOME:-$HOME/.vibesys}"
  install_dir="${VIBESYS_INSTALL_DIR:-$HOME/.local/bin}"
  releases_url="${VIBESYS_RELEASES_URL:-https://github.com/uw-syfi/vibesys/releases/download}"
  releases_url="${releases_url%/}"
  version="${VIBESYS_VERSION:-}"
  version="${version#v}"
  case "$version" in
    *[!0-9A-Za-z.-]*) fail "VIBESYS_VERSION '$version' is not a version such as 0.2.0" ;;
  esac

  # fail exits only the substitution it runs in, so each caller stops on its status.
  os="$(detect_os)" || exit 1
  arch="$(detect_arch "$os")" || exit 1

  mkdir -p "$home/releases" || fail "cannot create $home/releases"
  mkdir -p "$install_dir" || fail "cannot create $install_dir"
  # Refuse early to overwrite something that is not ours.
  if [ -e "$install_dir/vibesys-client" ] && [ ! -L "$install_dir/vibesys-client" ]; then
    fail "$install_dir/vibesys-client exists and is not a link made by this installer; move it away first"
  fi
  work="$(mktemp -d)" || fail "cannot create a temporary directory"
  stage="$home/releases/.stage.$$"
  created=""
  trap 'rm -rf "$work" "$stage"; [ -z "$created" ] || rm -rf "$created"' EXIT HUP INT TERM

  bundle=""
  unpacked=0
  if [ -n "${VIBESYS_INSTALL_BUNDLE:-}" ]; then
    step="reading the local bundle"
    bundle="$VIBESYS_INSTALL_BUNDLE"
    [ -f "$bundle" ] || fail "$bundle is not a file"
    if [ -f "$bundle.sha256" ]; then
      step="verifying the local bundle"
      verify_checksum "$bundle" "$bundle.sha256"
    fi
    unpack_bundle "$bundle"
    unpacked=1
    # The version is the bundle's own, which only its manifest holds.
    version="$(stage_version)" || exit 1
  elif [ -z "$version" ]; then
    # The newest release is the rolling vibesys-latest, whose VERSION file names it.
    step="finding the newest version"
    # vibesys-latest names stable versions only, so it does not exist while only pre-releases do.
    try_fetch "$releases_url/vibesys-latest/VERSION" "$work/VERSION" ||
      fail "cannot find the newest version at $releases_url/vibesys-latest/VERSION. No stable release may be published yet; name the version to install, for example VIBESYS_VERSION=0.2.0-rc.1"
    version="$(tr -d ' \n\r' <"$work/VERSION")"
    case "$version" in
      "" | *[!0-9A-Za-z.-]*) fail "the published VERSION file is not a version" ;;
    esac
  fi
  tag="vibesys-v$version"
  release="$home/releases/$version"

  if release_ready "$release"; then
    printf 'VibeSys %s is already unpacked at %s.\n' "$version" "$release"
  else
    if [ -z "$bundle" ]; then
      step="downloading VibeSys $version"
      asset="vibesys_${version}_${os}_${arch}.tar.gz"
      fetch "$releases_url/$tag/$asset.sha256" "$work/$asset.sha256"
      fetch "$releases_url/$tag/$asset" "$work/$asset"
      step="verifying the download"
      verify_checksum "$work/$asset" "$work/$asset.sha256"
      bundle="$work/$asset"
    fi

    if [ "$unpacked" = 0 ]; then
      unpack_bundle "$bundle"
      actual="$(stage_version)" || exit 1
      [ "$actual" = "$version" ] || fail "the bundle holds version $actual, not $version"
    fi

    step="moving the release into place"
    if [ -e "$release" ]; then
      # Left by an earlier failed install of this version: no environment, so nothing uses it.
      rm -rf "$release" || fail "cannot remove the incomplete $release"
    fi
    mv "$stage" "$release" || fail "cannot move the release into $home/releases"
    created="$release"

    step="finding uv"
    uv="$(find_uv)" || exit 1
    step="creating the sidecar environment (uv sync --frozen; this may download Python 3.12)"
    "$uv" sync --frozen --no-dev --python 3.12 --project "$release/sidecar" ||
      fail "uv could not create the sidecar environment in $release/sidecar"
    step="checking the installed release"
    "$release/bin/vibesys-client" release verify --installed "$release" >/dev/null || fail "$release is not complete after install"
    created=""
  fi

  step="stopping the running host"
  stop_host_for_switch "$version" "$release"
  step="switching to the new release"
  if [ "$(current_version)" != "$version" ]; then
    link_to "releases/$version" "$home/current"
  fi
  step="linking vibesys-client into $install_dir"
  link_to "$home/current/bin/vibesys-client" "$install_dir/vibesys-client"

  printf '\nInstalled VibeSys %s.\n  releases: %s\n  vibesys-client: %s/vibesys-client\n' "$version" "$home" "$install_dir"
  case ":${PATH:-}:" in
    *":$install_dir:"*) ;;
    *) printf '\n%s is not on your PATH. Add it to your shell profile, for example:\n  export PATH="%s:$PATH"\n' "$install_dir" "$install_dir" ;;
  esac
  cat <<EOF

Next steps, in the git repository that holds the task:
  vibesys-client run start --task <task> --detach
  vibesys-client run wait --until report --timeout 30m
  vibesys-client run get
  vibesys-client run stop --yes

What it needs:
  - git, because a task lives in a git repository.
  - Claude Code or Codex installed and logged in on this machine.
  - Docker, only for tasks that run in containers.

Agents run as your user, unconfined, with permissions bypassed: they can read and change anything
you can. Run VibeSys only on a machine and with tasks you trust.
EOF
}

main "$@"
