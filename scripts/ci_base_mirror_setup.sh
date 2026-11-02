#!/usr/bin/env bash
# Select the CI base image references for this job; never fail the job over a mirror.
set -uo pipefail
# Pin the credential directory for every later step. The test suite runs with a private
# HOME, so a Docker client there reads credentials only through DOCKER_CONFIG; the probe
# below reads them the same way, so it cannot pass for a consumer that would pull anonymously.
export DOCKER_CONFIG="${DOCKER_CONFIG:-${HOME:?}/.docker}"
echo "DOCKER_CONFIG=${DOCKER_CONFIG}" >> "${GITHUB_ENV:?}"
if ! printf '%s' "${GHCR_TOKEN:?token required}" |
    timeout --kill-after=5s 60s docker login ghcr.io --username "${GHCR_USER:?user required}" --password-stdin >/dev/null; then
    echo '::warning::GHCR login failed; CI base images fall back to the upstream registry'
fi
if resolution=$(timeout --kill-after=5s 240s python3 -m scripts.ci_base_images --resolve); then
    echo "VIBESYS_CI_BASE_IMAGES=${resolution}" >> "${GITHUB_ENV:?}"
else
    echo '::warning::CI base mirror resolution failed; using the upstream registry'
fi
exit 0
