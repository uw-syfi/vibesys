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

# fetch URL DEST: download one file. It returns 0 when the file arrived and 1 when the server says
# there is no such file (HTTP 404, or a file:// path that does not exist), so the caller can say
# what is missing; any other failure ends the script. The tool's own messages go to DEST.log, in
# the temporary directory, so a missing file is reported once, in words the caller chooses.
fetch() {
  log="$2.log"
  if command -v curl >/dev/null 2>&1; then
    # -w prints the status of the last response, after any redirect, even when -f fails the
    # transfer; it prints 000 when no HTTP response came. curl exits 37 for a file:// path it
    # cannot open.
    http_status="$(curl -fsSL -w '%{http_code}' -o "$2" "$1" 2>"$log")" && return 0
    tool_status=$?
    [ "$http_status" = 404 ] && return 1
    case "$1" in file://*) [ "$tool_status" -eq 37 ] && return 1 ;; esac
  elif command -v wget >/dev/null 2>&1; then
    # wget's exit status does not tell a missing file from a server error, so read the status
    # lines -S prints: "HTTP/1.1 404 Not Found" (GNU wget, and BusyBox's "server returned error:
    # HTTP/1.1 404 Not Found"). The last one counts, since a redirect prints one per response.
    LC_ALL=C wget -S -O "$2" "$1" 2>"$log" && return 0
    http_status="$(awk '{ for (i = 1; i < NF; i++) if ($i ~ /^HTTP\/[0-9.]+$/ && $(i + 1) ~ /^[0-9][0-9][0-9]$/) code = $(i + 1) } END { print code }' "$log")"
    [ "$http_status" = 404 ] && return 1
  else
    fail "needs curl or wget"
  fi
  detail="$(tail -n 1 "$log" 2>/dev/null)"
  fail "cannot download $1${detail:+ ($detail)}"
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
  default_releases_url=https://github.com/uw-syfi/vibesys/releases/download
  releases_url="${VIBESYS_CLI_RELEASES_URL:-$default_releases_url}"
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

  # Every release publishes checksums.txt, so a 404 for it means the release itself is missing.
  # A missing stable release is an error and never a quiet install of a pre-release instead.
  if ! fetch "$releases_url/$tag/checksums.txt" "$work/checksums.txt"; then
    if [ "$releases_url" = "$default_releases_url" ]; then
      listed=" (versions are listed at https://github.com/uw-syfi/vibesys/releases)"
    else
      listed=""
    fi
    case "$tag" in
      cli-latest) fail "no stable vibesys-cli release is published at $releases_url/$tag; to install a specific version, set VIBESYS_CLI_VERSION=<version>$listed" ;;
      *) fail "vibesys-cli version $version is not published: there is no release $tag at $releases_url; set VIBESYS_CLI_VERSION to a published version$listed" ;;
    esac
  fi
  fetch "$releases_url/$tag/$asset" "$work/$asset" ||
    fail "release $tag at $releases_url has no $asset, the binary for this machine"

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
