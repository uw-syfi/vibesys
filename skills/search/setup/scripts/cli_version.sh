#!/bin/sh
# Check a vibesys-cli against the versions the vibesys skills support, and find the newest
# release they support. POSIX sh; runs on Linux and macOS and needs curl or wget only for the
# release lookups.
#
#   cli_version.sh check [BINARY]     classify BINARY's version (default: $VIBESYS_CLI)
#   cli_version.sh classify VERSION   classify a version string
#   cli_version.sh newest             print the version cli-latest serves (what upgrade installs)
#   cli_version.sh newest-compatible  print the newest published stable release the skills support
#
# The supported range is caret (Cargo) semantics on the minimum version in
# ../assets/min-cli-version: for a major of 1 or more, the same major and at least the minimum;
# for major 0, the same minor and at least the minimum; for 0.0.z, exactly that version. A
# pre-release (0.1.0-rc.2) is below its release, and a pre-release of a version past the range
# is too new.
#
# check and classify print one line that starts with the status, and exit with:
#   0 ok        within the supported range
#   3 too-old   below the minimum
#   4 too-new   past the range
#   5 dev       a development build, which has no version to compare
#   6 unknown   the binary did not run, or its version could not be read
# newest and newest-compatible print the version and exit 0. A version counts only when its
# release is published: newest-compatible lists the cli-v* tags and, newest first, takes the
# first whose checksums.txt downloads (a 404 skips it). Every mode exits 1 when the minimum
# version file is unreadable, a lookup fails (unreachable, or any HTTP error but a skipped 404),
# or no published release qualifies, and 2 on a usage error.
#
# Settings, for a mirror or a test:
#   VIBESYS_CLI_TAGS_URL      the GitHub API listing of the cli-v* tags; the default is
#     https://api.github.com/repos/uw-syfi/vibesys/git/matching-refs/tags/cli-v
#   VIBESYS_CLI_RELEASES_URL  where release assets are served, as for install.sh; the default
#     is https://github.com/uw-syfi/vibesys/releases/download

set -u

tags_url="${VIBESYS_CLI_TAGS_URL:-https://api.github.com/repos/uw-syfi/vibesys/git/matching-refs/tags/cli-v}"
releases_url="${VIBESYS_CLI_RELEASES_URL:-https://github.com/uw-syfi/vibesys/releases/download}"
releases_url="${releases_url%/}"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)" || exit 1
min_file="$script_dir/../assets/min-cli-version"

fail() {
  printf 'cli_version.sh: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: cli_version.sh check [BINARY] | classify VERSION | newest | newest-compatible\n' >&2
  exit 2
}

# parse VERSION: set maj, mnr, pat and pre (empty for a release); fail on anything that is not
# MAJOR.MINOR.PATCH[-PRERELEASE] with an optional leading "v".
parse() {
  v="${1#v}"
  case "$v" in *"
"*) return 1 ;; esac
  printf '%s\n' "$v" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$' ||
    return 1
  core="${v%%-*}"
  pre=""
  [ "$core" = "$v" ] || pre="${v#*-}"
  maj="${core%%.*}"
  rest="${core#*.}"
  mnr="${rest%%.*}"
  pat="${rest#*.}"
}

# num_lt A B: true when the digit string A (no leading zeros) is below B, at any length.
num_lt() {
  [ "$1" = "$2" ] && return 1
  [ "${#1}" -ne "${#2}" ] && { [ "${#1}" -lt "${#2}" ]; return; }
  [ "$(printf '%s\n%s\n' "$1" "$2" | LC_ALL=C sort | head -n 1)" = "$1" ]
}

# core_lt A1 A2 A3 B1 B2 B3: true when core version A is below core version B.
core_lt() {
  [ "$1" != "$4" ] && { num_lt "$1" "$4"; return; }
  [ "$2" != "$5" ] && { num_lt "$2" "$5"; return; }
  num_lt "$3" "$6"
}

load_min() {
  [ -r "$min_file" ] || fail "cannot read the minimum version file $min_file"
  min="$(tr -d ' \t\r\n' < "$min_file")"
  parse "$min" && [ -z "$pre" ] && [ "${#maj}" -le 9 ] && [ "${#mnr}" -le 9 ] && [ "${#pat}" -le 9 ] ||
    fail "$min_file must hold one MAJOR.MINOR.PATCH version, found '$min'"
  min_maj="$maj" min_mnr="$mnr" min_pat="$pat"
  if [ "$min_maj" != 0 ]; then
    upper="$((min_maj + 1)).0.0"
  elif [ "$min_mnr" != 0 ]; then
    upper="0.$((min_mnr + 1)).0"
  else
    upper="0.0.$((min_pat + 1))"
  fi
  range=">=$min, <$upper"
}

# status_of VERSION: set status to ok, too-old, too-new, dev or unknown.
status_of() {
  if [ "$1" = dev ]; then
    status=dev
  elif ! parse "$1"; then
    status=unknown
  elif core_lt "$maj" "$mnr" "$pat" "$min_maj" "$min_mnr" "$min_pat"; then
    status=too-old
  elif [ "$maj.$mnr.$pat" = "$min" ] && [ -n "$pre" ]; then
    status=too-old
  elif [ "$maj" != "$min_maj" ] ||
    { [ "$min_maj" = 0 ] && [ "$mnr" != "$min_mnr" ]; } ||
    { [ "$min_maj" = 0 ] && [ "$min_mnr" = 0 ] && [ "$pat" != "$min_pat" ]; }; then
    status=too-new
  else
    status=ok
  fi
}

# report VERSION: print the status line for VERSION and exit with its code.
report() {
  status_of "$1"
  case "$status" in
    ok) printf 'ok: %s is within the supported range %s\n' "$1" "$range"; exit 0 ;;
    too-old) printf 'too-old: %s is below the supported range %s\n' "$1" "$range"; exit 3 ;;
    too-new) printf 'too-new: %s is past the supported range %s\n' "$1" "$range"; exit 4 ;;
    dev) printf 'dev: a development build, which cannot be checked against %s\n' "$range"; exit 5 ;;
    *) printf "unknown: '%s' is not a version; the supported range is %s\n" "$1" "$range"; exit 6 ;;
  esac
}

# binary_version BINARY: print the version BINARY reports, or nothing.
binary_version() {
  if out="$("$1" --version --json 2>/dev/null)"; then
    found="$(printf '%s\n' "$out" |
      sed -n 's/.*"binary_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
    if [ -n "$found" ]; then
      printf '%s\n' "$found"
      return
    fi
  fi
  if out="$("$1" --version 2>/dev/null)"; then
    printf '%s\n' "$out" | sed -n '1s/^vibesys-cli \([^ ]*\).*/\1/p'
  fi
}

check() {
  binary="${1:-${VIBESYS_CLI:-}}"
  [ -n "$binary" ] || usage
  version="$(binary_version "$binary")"
  if [ -z "$version" ]; then
    printf "unknown: '%s' did not run or reported no version; the supported range is %s\n" \
      "$binary" "$range"
    exit 6
  fi
  report "$version"
}

# get URL OUT: download URL into OUT and print the HTTP status (200, 404, ...). A transport
# failure prints nothing.
get() {
  if command -v curl >/dev/null 2>&1; then
    curl -sSL -o "$2" -w '%{http_code}' "$1" 2>/dev/null || return 0
  elif command -v wget >/dev/null 2>&1; then
    wget -q -S -O "$2" "$1" 2>"$2.headers"
    grep -o 'HTTP/[0-9.]* [0-9][0-9][0-9]' "$2.headers" | tail -n 1 | sed 's/.* //'
  else
    fail "needs curl or wget to look up releases"
  fi
}

# fetch URL OUT: download URL into OUT or fail. A 404 returns 1 so the caller can decide.
fetch() {
  code="$(get "$1" "$2")"
  case "$code" in
    200) return 0 ;;
    404) return 1 ;;
    "" | 000) fail "cannot reach $1" ;;
    *) fail "$1 answered HTTP $code" ;;
  esac
}

# newest: print the version cli-latest serves, which is what `vibesys-cli upgrade` installs.
newest() {
  fetch "$releases_url/cli-latest/VERSION" "$work/VERSION" ||
    fail "no stable release is published ($releases_url/cli-latest/VERSION is missing)"
  latest="$(tr -d ' \t\r\n' < "$work/VERSION")"
  parse "$latest" && [ -z "$pre" ] ||
    fail "$releases_url/cli-latest/VERSION holds '$latest', not a stable version"
  printf '%s\n' "${latest#v}"
}

# newest_compatible: print the newest supported stable version whose release is published. A tag
# alone is not enough: its release may have failed, and then the installer's download 404s.
newest_compatible() {
  fetch "$tags_url" "$work/tags" || fail "cannot list releases: $tags_url is missing"
  candidates=""
  for tag in $(grep -o '"ref"[[:space:]]*:[[:space:]]*"refs/tags/cli-v[^"]*"' "$work/tags" |
    sed 's#.*refs/tags/cli-v##; s#"$##'); do
    parse "$tag" && [ -z "$pre" ] || continue
    status_of "$tag"
    [ "$status" = ok ] && candidates="$candidates $tag"
  done
  # Newest first: take the highest candidate left, probe its release, and drop it if missing.
  while [ -n "$candidates" ]; do
    best="" rest=""
    for tag in $candidates; do
      parse "$tag"
      if [ -z "$best" ] || core_lt "$best_maj" "$best_mnr" "$best_pat" "$maj" "$mnr" "$pat"; then
        [ -z "$best" ] || rest="$rest $best"
        best="$tag" best_maj="$maj" best_mnr="$mnr" best_pat="$pat"
      else
        rest="$rest $tag"
      fi
    done
    if fetch "$releases_url/cli-v$best/checksums.txt" "$work/checksums.txt"; then
      printf '%s\n' "$best"
      return 0
    fi
    candidates="$rest"
  done
  fail "no published stable release is within the supported range $range"
}

make_work() {
  work="$(mktemp -d)" || fail "cannot create a temporary directory"
  trap 'rm -rf "$work"' EXIT HUP INT TERM
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift
case "$mode" in
  check) [ "$#" -le 1 ] || usage; load_min; check "$@" ;;
  classify) [ "$#" -eq 1 ] || usage; load_min; report "$1" ;;
  newest) [ "$#" -eq 0 ] || usage; load_min; make_work; newest ;;
  newest-compatible) [ "$#" -eq 0 ] || usage; load_min; make_work; newest_compatible ;;
  *) usage ;;
esac
