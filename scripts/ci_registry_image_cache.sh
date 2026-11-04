#!/usr/bin/env bash
# GHCR is shared by PR and merge-queue refs; actions/cache entries are not.
# The local archive format stays owned by ci_docker_data_root.sh.
set -euo pipefail

case ${CACHE_MODE:?cache mode required} in
    restore|save) ;;
    *) echo "unknown cache mode: ${CACHE_MODE}" >&2; exit 2 ;;
esac
reference() {
    python3 scripts/ci_image_cache.py "${GITHUB_REPOSITORY}" "$1"
}

primary=$(reference "${CACHE_KEY:?cache key required}")
printf '%s' "${CACHE_TOKEN:?registry token required}" |
    timeout --kill-after=5s 60s docker login ghcr.io --username "${GITHUB_ACTOR}" --password-stdin >/dev/null
cache_container=''
cache_context=''

cleanup() {
    if [[ -n ${cache_container} ]]; then
        timeout --kill-after=5s 60s docker rm -f "${cache_container}" >/dev/null 2>&1 || true
    fi
    if [[ -n ${cache_context} ]]; then rm -rf "${cache_context}"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ensure_package() {
    # GHCR may deny a pull of a package that has never been published. Create
    # its metadata-only marker first, rather than treating denied access to an
    # existing package as a cache miss. This build has no install commands.
    [[ ${CACHE_CAN_PUBLISH:-false} == true ]] || return 0
    local marker="${primary%:*}:bootstrap"
    if timeout --kill-after=5s 60s docker manifest inspect "${marker}" >/dev/null 2>&1; then
        return
    fi
    cache_context=$(mktemp -d "${RUNNER_TEMP}/image-cache-bootstrap-XXXXXX")
    printf 'CI image cache\n' > "${cache_context}/marker"
    printf 'FROM scratch\nLABEL org.opencontainers.image.source="https://github.com/%s"\nCOPY marker /marker\n' \
        "${GITHUB_REPOSITORY}" > "${cache_context}/Dockerfile"
    timeout --kill-after=5s 60s docker build --provenance=false --tag "${marker}" "${cache_context}"
    timeout --kill-after=5s 60s docker push "${marker}"
    rm -rf "${cache_context}"
    cache_context=''
    timeout --kill-after=5s 60s docker image rm "${marker}" >/dev/null
}

# The cache image is several gigabytes and a healthy pull measured about 171 s on a
# runner, so the former 180 s bound failed merge groups whenever the registry was a
# little slow. 270 s is 1.6x the measurement. A timed-out pull is retried once and
# Docker keeps the layers it finished, so the retry needs less than a full pull; the
# worst case of 2 x 270 s still fits the 600 s merge-group budget
# (CI_WALL_TIME_ERROR_SECONDS) and the 10 minute job limits of the cache consumers.
pull_timeout_seconds=270
pull_attempts=2

note() {
    echo "$1" >&2
    echo "$1" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

# Pull one image. Returns 0 when pulled, 1 when the registry has no such image,
# 2 when the pull failed for a reason that must fail the job. Only a timeout or a
# transient network error is retried; denied access and an outage are not.
pull_image() {
    local ref=$1 log=$2 attempt=1 status why
    while :; do
        status=0
        timeout --kill-after=5s "${pull_timeout_seconds}s" docker pull "${ref}" >"${log}" 2>&1 || status=$?
        if ((status == 0)); then return 0; fi
        if grep -Eq 'manifest unknown|manifest for .* not found|ghcr[.]io/[^[:space:]]+: not found' "${log}"; then
            return 1
        fi
        if ((status == 124 || status == 137)); then
            why="cache pull timed out after ${pull_timeout_seconds} s (attempt ${attempt} of ${pull_attempts}): ${ref}"
        elif grep -Eiq 'TLS handshake timeout|i/o timeout|connection reset|unexpected EOF|Client[.]Timeout|received unexpected HTTP status: 5[0-9][0-9]' "${log}"; then
            why="cache pull hit a transient network error (attempt ${attempt} of ${pull_attempts}): ${ref}"
        elif grep -Eiq 'denied|unauthorized|forbidden' "${log}"; then
            cat "${log}" >&2
            note "cache pull denied by the registry, not retried: ${ref}"
            return 2
        else
            # A registry outage must fail here, not silently turn an unchanged
            # build into another cold apt install.
            cat "${log}" >&2
            note "cache registry outage, docker pull exited ${status}, not retried: ${ref}"
            return 2
        fi
        note "${why}"
        if ((attempt >= pull_attempts)); then
            cat "${log}" >&2
            note "cache pull failed after ${attempt} attempts: ${why}"
            return 2
        fi
        attempt=$((attempt + 1))
    done
}

restore() {
    ensure_package
    local key ref pulled
    local log="${RUNNER_TEMP}/image-cache-pull.log"
    while IFS= read -r key; do
        [[ -n ${key} ]] || continue
        ref=$(reference "${key}")
        pulled=0
        pull_image "${ref}" "${log}" || pulled=$?
        if ((pulled == 2)); then return 1; fi
        if ((pulled == 0)); then
            cache_container=$(timeout --kill-after=5s 60s docker create "${ref}" /unused)
            timeout --kill-after=5s 180s docker cp "${cache_container}:/docker-data.tar.zst" "${CACHE_PATH}"
            timeout --kill-after=5s 60s docker rm "${cache_container}" >/dev/null
            cache_container=''
            timeout --kill-after=5s 60s docker image rm "${ref}" >/dev/null
            echo "Restored image cache ${ref}"
            if [[ ${key} == "${CACHE_KEY}" ]]; then
                echo 'cache-hit=true' >> "${GITHUB_OUTPUT}"
            else
                echo 'cache-hit=false' >> "${GITHUB_OUTPUT}"
            fi
            return
        fi
    done <<< "${CACHE_KEY}
${CACHE_RESTORE_KEYS:-}"
    echo 'No image cache exists for these install inputs or bases'
    echo 'cache-hit=false' >> "${GITHUB_OUTPUT}"
}

save() {
    local alias key
    cache_context=$(mktemp -d "${RUNNER_TEMP}/image-cache-publish-XXXXXX")
    # A hard link avoids a second multi-gigabyte copy on the runner filesystem.
    ln "${CACHE_PATH}" "${cache_context}/docker-data.tar.zst"
    cat > "${cache_context}/Dockerfile" <<'DOCKERFILE'
FROM scratch
ARG SOURCE
LABEL org.opencontainers.image.source=$SOURCE
COPY docker-data.tar.zst /docker-data.tar.zst
DOCKERFILE
    timeout --kill-after=5s 180s docker build --provenance=false \
        --build-arg "SOURCE=https://github.com/${GITHUB_REPOSITORY}" \
        --tag "${primary}" "${cache_context}"
    timeout --kill-after=5s 180s docker push "${primary}"
    while IFS= read -r key; do
        [[ -n ${key} ]] || continue
        alias=$(reference "${key}")
        timeout --kill-after=5s 60s docker tag "${primary}" "${alias}"
        timeout --kill-after=5s 180s docker push "${alias}"
    done <<< "${CACHE_RESTORE_KEYS:-}"
}

"${CACHE_MODE}"
