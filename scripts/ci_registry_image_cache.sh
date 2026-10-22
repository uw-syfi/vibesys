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

restore() {
    ensure_package
    local key ref
    local log="${RUNNER_TEMP}/image-cache-pull.log"
    while IFS= read -r key; do
        [[ -n ${key} ]] || continue
        ref=$(reference "${key}")
        if timeout --kill-after=5s 180s docker pull "${ref}" >"${log}" 2>&1; then
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
        # A registry outage or denied access must fail here, not silently turn
        # an unchanged build into another cold apt install.
        if ! grep -Eq 'manifest unknown|manifest for .* not found|ghcr[.]io/[^[:space:]]+: not found' "${log}"; then
            cat "${log}" >&2
            return 1
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
