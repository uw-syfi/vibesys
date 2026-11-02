#!/usr/bin/env bash
# Cache storage is optional. Required image builds run outside this action.
set -euo pipefail
# Bound the entire cache attempt so a stalled registry leaves time for a cold build.
cache_seconds=120
if [[ ${CACHE_MODE} == restore ]]; then cache_seconds=60; fi
if ! timeout --kill-after=5s "${cache_seconds}s" python3 -m scripts.ci_registry_image_cache; then
    echo "::warning::Image cache ${CACHE_MODE} failed; continuing without the cache"
    if [[ ${CACHE_MODE} == restore ]]; then
        rm -f "${CACHE_PATH}"
        echo 'cache-hit=false' >> "${GITHUB_OUTPUT}"
    fi
fi
