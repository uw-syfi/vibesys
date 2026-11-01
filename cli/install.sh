#!/bin/sh
# Install vibesys-cli from GitHub Releases:
#
#   curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/cli/install.sh | sh
#
# It picks the binary for this operating system and architecture, checks it against the release's
# published SHA-256, and installs it into a directory you own. It never uses root and never edits
# a shell profile. Settings, all optional:
#
#   VIBESYS_CLI_VERSION      install this version (for example 0.2.0) instead of the newest
#   VIBESYS_CLI_INSTALL_DIR  install here instead of $HOME/.local/bin
#   VIBESYS_CLI_RELEASES_URL where release assets are served, for a mirror or a test; the default
#                            is https://github.com/uw-syfi/vibesys/releases/download
#
# The whole script is a function called on the last line, so a download cut short does nothing.

fail() {
  printf 'install.sh: %s\n' "$*" >&2
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

main() {
  releases_url="${VIBESYS_CLI_RELEASES_URL:-https://github.com/uw-syfi/vibesys/releases/download}"
  releases_url="${releases_url%/}"
  if [ -n "${VIBESYS_CLI_INSTALL_DIR:-}" ]; then
    install_dir="$VIBESYS_CLI_INSTALL_DIR"
  else
    [ -n "${HOME:-}" ] || fail "HOME is not set; set VIBESYS_CLI_INSTALL_DIR"
    install_dir="$HOME/.local/bin"
  fi

  # The tag of the newest release is the rolling cli-latest, so no version has to be resolved
  # first. GitHub's own "latest" marker is not used: other releases of this repository share it.
  version="${VIBESYS_CLI_VERSION:-}"
  version="${version#v}"
  case "$version" in
    *[!0-9A-Za-z.-]*) fail "VIBESYS_CLI_VERSION '$version' is not a version such as 0.2.0" ;;
    "") tag=cli-latest ;;
    *) tag="cli-v$version" ;;
  esac

  os="$(detect_os)"
  arch="$(detect_arch "$os")"
  asset="vibesys-cli_${os}_${arch}"

  work="$(mktemp -d)" || fail "cannot create a temporary directory"
  staged=""
  trap 'rm -rf "$work"; [ -z "$staged" ] || rm -f "$staged"' EXIT HUP INT TERM

  fetch "$releases_url/$tag/checksums.txt" "$work/checksums.txt"
  fetch "$releases_url/$tag/$asset" "$work/$asset"

  # The checksum line for exactly this file, once. sha256sum marks binary mode with a "*".
  expected="$(awk -v name="$asset" '{ n = $2; sub(/^\*/, "", n) } n == name { print $1 }' "$work/checksums.txt")"
  case "$expected" in
    "") fail "checksums.txt has no entry for $asset" ;;
    *[!0-9a-f]* | *"
"*) fail "checksums.txt has a malformed or repeated entry for $asset" ;;
  esac
  [ "${#expected}" -eq 64 ] || fail "checksums.txt has a malformed entry for $asset"
  actual="$(sha256_of "$work/$asset")"
  [ "$actual" = "$expected" ] ||
    fail "$asset does not match its published checksum (got $actual, expected $expected); nothing was installed"

  mkdir -p "$install_dir" || fail "cannot create $install_dir"
  staged="$install_dir/.vibesys-cli.$$"
  cp "$work/$asset" "$staged" || fail "cannot write to $install_dir"
  chmod 755 "$staged"
  # Run the staged file once: a binary that cannot start here is not installed over a working one.
  if ! reported="$("$staged" --version 2>&1)"; then
    fail "the downloaded binary does not run on this machine: $reported"
  fi
  mv -f "$staged" "$install_dir/vibesys-cli" || fail "cannot install into $install_dir"
  staged=""

  printf '%s\nInstalled to %s/vibesys-cli\n' "$reported" "$install_dir"
  case ":${PATH:-}:" in
    *":$install_dir:"*) ;;
    *) printf 'Add %s to your PATH to run it by name.\n' "$install_dir" ;;
  esac
}

main "$@"
