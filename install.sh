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
#   --force                  switch versions even while a vibesys-host of the current version runs
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
# same version again changes nothing. A switch to another version is refused while a vibesys-host
# started from a release under $VIBESYS_HOME is running, unless --force is given; see running_host.
#
# The whole script is a function called on the last line, so a download cut short does nothing.

step="starting"

fail() {
  printf 'install.sh: failed while %s: %s\nThe previous install, if any, is unchanged.\n' "$step" "$*" >&2
  exit 1
}

# fetch URL DEST: download one file or fail.
fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$2" "$1" || fail "cannot download $1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1" || fail "cannot download $1"
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

# detect_arch OS: print amd64 or arm64.
detect_arch() {
  case "$(uname -m)" in
    x86_64 | amd64)
      # A shell running under Rosetta reports x86_64 on an Apple Silicon Mac.
      if [ "$1" = darwin ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
        echo arm64
      else
        echo amd64
      fi
      ;;
    aarch64 | arm64) echo arm64 ;;
    *) fail "unsupported architecture '$(uname -m)'; releases cover amd64 and arm64" ;;
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

# release_ready DIR: DIR is a complete release, with a sidecar environment that imports.
release_ready() {
  [ -x "$1/bin/vibesys-client" ] && [ -x "$1/sidecar/.venv/bin/python" ] &&
    "$1/sidecar/.venv/bin/python" -c 'import vs_sidecar' >/dev/null 2>&1
}

# current_version: the version `current` points at, or nothing.
current_version() {
  if [ -L "$home/current" ]; then
    basename "$(readlink "$home/current")"
  fi
}

# running_host: print the path of a vibesys-host process started from a release under $home, if
# any. The test is the process list: ps shows each process's command line, and a host started from
# `current/bin` or `releases/<version>/bin` has a command line that starts with that path. Hosts
# started from elsewhere (a development build) are not ours and are not considered. The list holds
# the old version's processes because the old version is the only one that has run so far.
running_host() {
  ps -axo args= 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      "$home"/*/bin/vibesys-host | "$home"/*/bin/vibesys-host\ *)
        printf '%s\n' "${line%% *}"
        break
        ;;
    esac
  done
}

# refuse_if_serving: stop before moving `current` to another version while a host runs.
refuse_if_serving() {
  [ "$force" = 0 ] || return 0
  [ -n "$(current_version)" ] && [ "$(current_version)" != "$1" ] || return 0
  running="$(running_host)"
  [ -z "$running" ] ||
    fail "a vibesys-host is running ($running). Stop it, or run again with --force to switch anyway; agents already running keep using the old release, which is left in place"
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

  os="$(detect_os)"
  arch="$(detect_arch "$os")"
  # The sidecar depends on agentshim by git URL, so uv needs git to build its environment.
  command -v git >/dev/null 2>&1 || fail "git is not installed; it is needed to fetch the sidecar's dependencies. Install git and run this again"

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
  if [ -n "${VIBESYS_INSTALL_BUNDLE:-}" ]; then
    step="reading the local bundle"
    bundle="$VIBESYS_INSTALL_BUNDLE"
    [ -f "$bundle" ] || fail "$bundle is not a file"
    if [ -f "$bundle.sha256" ]; then
      step="verifying the local bundle"
      verify_checksum "$bundle" "$bundle.sha256"
    fi
    step="reading the local bundle"
    version="$(tar -xzOf "$bundle" ./VERSION 2>/dev/null)" || fail "$bundle is not a readable VibeSys bundle"
  elif [ -z "$version" ]; then
    # The newest release is the rolling vibesys-latest, whose VERSION file names it.
    step="finding the newest version"
    fetch "$releases_url/vibesys-latest/VERSION" "$work/VERSION"
    version="$(tr -d ' \n\r' <"$work/VERSION")"
    case "$version" in
      "" | *[!0-9A-Za-z.-]*) fail "the published VERSION file is not a version" ;;
    esac
  fi
  tag="vibesys-v$version"
  release="$home/releases/$version"

  step="checking for a running host"
  refuse_if_serving "$version"

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

    step="unpacking the bundle"
    # Reject absolute and parent-relative names before extracting anything.
    tar -tzf "$bundle" >"$work/names" 2>/dev/null || fail "$bundle is not a readable VibeSys bundle"
    if grep -E '^/|(^|/)\.\.(/|$)' "$work/names" >/dev/null; then
      fail "the bundle contains paths outside its own directory"
    fi
    rm -rf "$stage"
    mkdir -p "$stage" || fail "cannot create $stage"
    tar -xzf "$bundle" -C "$stage" || fail "cannot extract the bundle"
    for needed in bin/vibesys-client bin/vibesys-host bin/vibesys-cli sidecar/pyproject.toml \
      sidecar/uv.lock marketplace/.claude-plugin/marketplace.json VERSION; do
      [ -e "$stage/$needed" ] || fail "the bundle has no $needed"
    done
    [ "$(tr -d ' \n\r' <"$stage/VERSION")" = "$version" ] || fail "the bundle's VERSION is not $version"

    step="checking the binaries run here"
    # A binary that cannot start on this machine is not installed over a working one.
    if ! "$stage/bin/vibesys-client" --version >/dev/null 2>&1 &&
      ! "$stage/bin/vibesys-client" --help >/dev/null 2>&1; then
      fail "the bundle's vibesys-client does not run on this machine"
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
    step="checking the sidecar environment"
    release_ready "$release" || fail "the sidecar environment in $release cannot import vs_sidecar"
    created=""
  fi

  step="switching to the new release"
  refuse_if_serving "$version"
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

Next steps:
  vibesys-client run start --task <task>

What it needs:
  - Claude Code or Codex installed and logged in on this machine.
  - Docker, only for tasks that run in containers.

Agents run as your user, unconfined, with permissions bypassed: they can read and change anything
you can. Run VibeSys only on a machine and with tasks you trust.
EOF
}

main "$@"
