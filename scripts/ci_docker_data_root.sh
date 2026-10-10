#!/usr/bin/env bash
# CI helper: keep the Docker daemon's image and build-cache storage in a
# tarball that survives between runs, so `docker build` layers are reused.
#
#   ci_docker_data_root.sh attach   <tarball>   point dockerd at /mnt/docker-data, seeded from <tarball> if it exists
#   ci_docker_data_root.sh snapshot <tarball>   drop this run's images and containers, then write the storage to <tarball>
#
# The tests build through plain `docker build` against the daemon's own
# builder, which cannot import or export buildx cache flags, so the cache has
# to live in the daemon's storage. Only CI calls this script.
set -euo pipefail

readonly DATA_ROOT=/mnt/docker-data
readonly DAEMON_JSON=/etc/docker/daemon.json

stop_docker() {
    sudo systemctl stop docker.service docker.socket
}

start_docker() {
    sudo systemctl start docker.service
}

attach() {
    local tarball=$1
    stop_docker
    sudo mkdir -p "${DATA_ROOT}"
    if [[ -s ${tarball} ]]; then
        sudo tar --use-compress-program=unzstd -xf "${tarball}" -C "${DATA_ROOT}"
    fi
    local current='{}'
    if [[ -f ${DAEMON_JSON} ]]; then
        current=$(sudo cat "${DAEMON_JSON}")
    fi
    jq --arg root "${DATA_ROOT}" '. + {"data-root": $root}' <<<"${current}" |
        sudo tee "${DAEMON_JSON}" >/dev/null
    start_docker
    docker info --format 'docker root: {{.DockerRootDir}}'
    df -h "${DATA_ROOT}"
}

snapshot() {
    local tarball=$1
    # Keep layers and the build cache; drop what this run created. Tagged
    # test images would otherwise inflate the cache and are rebuilt from it.
    docker ps -aq | xargs -r docker rm -f >/dev/null
    docker images --format '{{.Repository}}:{{.Tag}}' | { grep '^vibesys-' || true; } |
        xargs -r docker rmi -f >/dev/null
    docker image prune -f >/dev/null
    stop_docker
    sudo tar --use-compress-program='zstd -T0 -3' -cf "${tarball}" -C "${DATA_ROOT}" .
    sudo chown "$(id -u):$(id -g)" "${tarball}"
    ls -lh "${tarball}"
    start_docker
}

case ${1:-} in
    attach) attach "${2:?tarball path required}" ;;
    snapshot) snapshot "${2:?tarball path required}" ;;
    *)
        echo "usage: $0 attach|snapshot <tarball>" >&2
        exit 2
        ;;
esac
