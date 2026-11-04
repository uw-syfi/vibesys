#!/usr/bin/env bash
# Runs scripts/ci_registry_image_cache.sh restore against a fake `docker` and `timeout`
# on PATH, so no registry is needed. The fake docker answers each `docker pull` from a
# scripted list of outcomes; the fake timeout records its bound and runs the command.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/bin"

cat > "$work/bin/timeout" <<'FAKE'
#!/usr/bin/env bash
shift  # --kill-after=...
echo "$1 $2 $3" >> "${FAKE_DIR}/timeouts"
shift
exec "$@"
FAKE

cat > "$work/bin/docker" <<'FAKE'
#!/usr/bin/env bash
case $1 in
    pull)
        n=$(($(cat "${FAKE_DIR}/pulls" 2>/dev/null || echo 0) + 1))
        echo "$n" > "${FAKE_DIR}/pulls"
        echo "$2" >> "${FAKE_DIR}/refs"
        outcome=$(sed -n "${n}p" "${FAKE_DIR}/script")
        case $outcome in
            ok) echo 'Pulling fs layer'; exit 0 ;;
            timeout) echo 'Pulling fs layer'; exit 124 ;;
            killed) echo 'Pulling fs layer'; exit 137 ;;
            reset) echo 'read tcp: connection reset by peer'; exit 1 ;;
            denied) echo 'denied: permission_denied: read_package'; exit 1 ;;
            outage) echo 'received unexpected HTTP status: 404'; echo 'dial tcp: no route'; exit 1 ;;
            notfound) echo "manifest unknown: manifest unknown"; exit 1 ;;
        esac ;;
    create) echo fake-container ;;
    cp) : > "$3" ;;
esac
exit 0
FAKE
chmod +x "$work/bin/"*

# run <name> <outcomes...>: sets status, stdout+stderr in $out, summary in $summary.
run() {
    local name=$1; shift
    FAKE_DIR="$work/$name"
    mkdir "$FAKE_DIR"
    printf '%s\n' "$@" > "$FAKE_DIR/script"
    : > "$FAKE_DIR/summary"; : > "$FAKE_DIR/output"
    status=0
    (
        cd "$root"
        PATH="$work/bin:$PATH" FAKE_DIR="$FAKE_DIR" CACHE_MODE=restore \
            CACHE_KEY=key-a CACHE_RESTORE_KEYS=$'key-b\nkey-c' CACHE_TOKEN=t \
            CACHE_PATH="$FAKE_DIR/archive" GITHUB_REPOSITORY=owner/repo GITHUB_ACTOR=a \
            RUNNER_TEMP="$FAKE_DIR" GITHUB_OUTPUT="$FAKE_DIR/output" \
            GITHUB_STEP_SUMMARY="$FAKE_DIR/summary" \
            ./scripts/ci_registry_image_cache.sh
    ) > "$FAKE_DIR/log" 2>&1 || status=$?
    pulls=$(cat "$FAKE_DIR/pulls" 2>/dev/null || echo 0)
}
expect() { # expect <condition description> <command...>
    local what=$1; shift
    "$@" || { echo "FAIL ${name}: ${what}" >&2; cat "$FAKE_DIR/log" >&2; exit 1; }
}
has() { grep -Fq -- "$2" "$FAKE_DIR/$1"; }
lacks() { ! grep -Fq -- "$2" "$FAKE_DIR/$1"; }

name=slow-pull-succeeds-on-retry
run $name timeout ok
expect 'exits 0' test "$status" -eq 0
expect 'pulled twice' test "$pulls" -eq 2
expect 'restored' has log 'Restored image cache'
expect 'cache hit' has output 'cache-hit=true'
expect 'timeout named' has summary 'cache pull timed out after 270 s (attempt 1 of 2)'
expect 'bound is 270 s' has timeouts '270s docker pull'

name=killed-pull-counts-as-timeout
run $name killed ok
expect 'exits 0' test "$status" -eq 0
expect 'timeout named' has summary 'timed out after 270 s (attempt 1 of 2)'

name=transient-network-error-retried
run $name reset ok
expect 'exits 0' test "$status" -eq 0
expect 'named' has summary 'transient network error (attempt 1 of 2)'

name=timeout-twice-fails
run $name timeout timeout ok
expect 'fails' test "$status" -ne 0
expect 'two pulls only' test "$pulls" -eq 2
expect 'first attempt' has summary 'cache pull timed out after 270 s (attempt 1 of 2)'
expect 'second attempt' has summary 'cache pull timed out after 270 s (attempt 2 of 2)'
expect 'final' has summary 'cache pull failed after 2 attempts'
expect 'no cache hit' lacks output 'cache-hit'

name=denied-fails-without-retry
run $name denied ok
expect 'fails' test "$status" -ne 0
expect 'one pull' test "$pulls" -eq 1
expect 'named' has summary 'cache pull denied by the registry'
expect 'not a timeout' lacks summary 'timed out'

name=outage-fails-without-retry
run $name outage ok
expect 'fails' test "$status" -ne 0
expect 'one pull' test "$pulls" -eq 1
expect 'named' has summary 'cache registry outage'

name=not-found-falls-through-to-next-key
run $name notfound notfound ok
expect 'exits 0' test "$status" -eq 0
expect 'three keys tried' test "$pulls" -eq 3
expect 'restored a fallback' has log 'Restored image cache'
expect 'fallback is not a hit' has output 'cache-hit=false'
expect 'no summary noise' test ! -s "$FAKE_DIR/summary"

name=not-found-everywhere-is-cold
run $name notfound notfound notfound
expect 'exits 0' test "$status" -eq 0
expect 'cold path' has log 'No image cache exists'
expect 'not a hit' has output 'cache-hit=false'

echo 'ci registry image cache: pull retry, classification and fall-through hold'
