#!/usr/bin/env bash
# Run `devcontainer features test` for the Features in one or more projects on
# one base image.
#
#   test_features.sh <base-image> <project-folder>...
#
# A project folder holds `src/<id>/` and `test/<id>/` for each Feature, the
# layout `devcontainer features test` expects; every Feature owner has one.
# This is the authoring and CI path only. A run never uses the CLI: it builds
# the image with `docker build` from a Dockerfile `vs_sandbox.feature_dockerfile`
# generates, so a run needs no Docker socket mount. Here the CLI (built from the
# Dockerfile beside this script, so the tool is pinned) drives the host's Docker
# daemon through the mounted socket and reads each project at the same absolute
# path the host sees; run it on a host that allows that mount (CI runners do).
# It checks that the Features conform to the specification the generator implements.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <base-image> <project-folder>..." >&2
    exit 2
fi
base=$1
shift

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Build from the CI mirror when this job resolved one; otherwise the Dockerfile's
# default is the same pinned digest upstream.
cli_base=$(cd "${here}" && cd "$(git rev-parse --show-toplevel)" &&
    python3 -m scripts.ci_base_images node:24.21.0-bookworm-slim 2>/dev/null) || cli_base=''
build_args=()
if [[ -n ${cli_base} ]]; then build_args=(--build-arg "BASE_IMAGE=${cli_base}"); fi
docker build --quiet --tag vibesys-devcontainer-cli:features-test \
    ${build_args[@]+"${build_args[@]}"} --file "${here}/Dockerfile" "${here}" >/dev/null

# `features test` stages each test project under this directory and hands the
# path to the host daemon as a bind mount source, so it must be the same
# directory in the CLI container and on the host.
scratch=/tmp/devcontainercli
mkdir -p "${scratch}"

for project in "$@"; do
    project=$(cd "${project}" && pwd)
    echo "== features in ${project} on ${base}"
    docker run --rm \
        --volume /var/run/docker.sock:/var/run/docker.sock \
        --volume "${project}:${project}" \
        --volume "${scratch}:${scratch}" \
        vibesys-devcontainer-cli:features-test \
        features test --project-folder "${project}" --base-image "${base}"
done
