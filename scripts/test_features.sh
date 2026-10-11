#!/usr/bin/env bash
# Run `devcontainer features test` for every VibeSys Feature on one base image.
#
#   scripts/test_features.sh <base-image> [feature-id ...]
#
# The CLI is the containerized one `agent_image` uses (built from
# libs/vs-agent/src/vs_agent/images/devcontainer-cli/Dockerfile), so CI and a
# run exercise the same pinned tool and the host needs only Docker. It drives
# the host's Docker daemon through the mounted socket and reads the project at
# the same absolute path the host sees.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <base-image> [feature-id ...]" >&2
    exit 2
fi
base=$1
shift

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
images=${repo}/libs/vs-agent/src/vs_agent/images
project=${images}/features

docker build --quiet --tag vibesys-devcontainer-cli:features-test \
    --file "${images}/devcontainer-cli/Dockerfile" "${images}/devcontainer-cli" >/dev/null

# `features test` stages each test project under this directory and hands the
# path to the host daemon as a bind mount source, so it must be the same
# directory in the CLI container and on the host.
scratch=/tmp/devcontainercli
mkdir -p "${scratch}"

only=()
for feature in "$@"; do
    only+=(--features "${feature}")
done

exec docker run --rm \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "${project}:${project}" \
    --volume "${scratch}:${scratch}" \
    vibesys-devcontainer-cli:features-test \
    features test --project-folder "${project}" --base-image "${base}" "${only[@]}"
