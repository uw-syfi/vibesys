#!/bin/sh
# Check a vibesys-cli against the versions the vibesys skills support, and find the newest
# release they support. POSIX sh; runs on Linux and macOS and needs curl or wget only for the
# release lookups.
#
#   cli_version.sh check [BINARY]     classify BINARY's version (default: $VIBESYS_CLI)
#   cli_version.sh classify VERSION   classify a version string
#   cli_version.sh newest             print the newest stable release
#   cli_version.sh newest-compatible  print the newest stable release the skills support
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
# newest and newest-compatible print the version and exit 0. Every mode exits 1 when the
# minimum version file is unreadable, a release lookup fails, or no release qualifies, and 2 on
# a usage error.
#
# Settings, for a mirror or a test:
#   VIBESYS_CLI_TAGS_URL  the GitHub API listing of the cli-v* tags; the default is
#     https://api.github.com/repos/uw-syfi/vibesys/git/matching-refs/tags/cli-v

set -u

tags_url="${VIBESYS_CLI_TAGS_URL:-https://api.github.com/repos/uw-syfi/vibesys/git/matching-refs/tags/cli-v}"
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

fetch_tags() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$tags_url" || fail "cannot list releases from $tags_url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O - "$tags_url" || fail "cannot list releases from $tags_url"
  else
    fail "needs curl or wget to list releases"
  fi
}

# newest COMPATIBLE: print the newest stable release, only among supported ones when COMPATIBLE
# is "yes".
newest() {
  listing="$(fetch_tags)" || exit 1
  best=""
  for tag in $(printf '%s\n' "$listing" |
    grep -o '"ref"[[:space:]]*:[[:space:]]*"refs/tags/cli-v[^"]*"' |
    sed 's#.*refs/tags/cli-v##; s#"$##'); do
    parse "$tag" && [ -z "$pre" ] || continue
    if [ "$1" = yes ]; then
      status_of "$tag"
      [ "$status" = ok ] || continue
      parse "$tag"
    fi
    if [ -z "$best" ] || core_lt "$best_maj" "$best_mnr" "$best_pat" "$maj" "$mnr" "$pat"; then
      best="$tag" best_maj="$maj" best_mnr="$mnr" best_pat="$pat"
    fi
  done
  if [ -z "$best" ]; then
    if [ "$1" = yes ]; then
      fail "no stable release is within the supported range $range"
    fi
    fail "no stable release is listed at $tags_url"
  fi
  printf '%s\n' "$best"
}

[ "$#" -ge 1 ] || usage
mode="$1"
shift
case "$mode" in
  check) [ "$#" -le 1 ] || usage; load_min; check "$@" ;;
  classify) [ "$#" -eq 1 ] || usage; load_min; report "$1" ;;
  newest) [ "$#" -eq 0 ] || usage; load_min; newest no ;;
  newest-compatible) [ "$#" -eq 0 ] || usage; load_min; newest yes ;;
  *) usage ;;
esac
