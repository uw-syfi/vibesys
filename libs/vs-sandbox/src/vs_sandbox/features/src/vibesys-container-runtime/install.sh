#!/usr/bin/env bash
# The container-topology tools as a Dev Container Feature. Container-topology
# tasks (compose or kind candidates) run their own Docker daemon inside the
# sandbox container, started at container start by `vs_sandbox` under the
# Sysbox runtime. Only the tools land here: static engine binaries (no apt
# repository key to manage), the compose plugin, kind, and kubectl, plus the
# `docker` group that owns the daemon socket. The caller names, in the `users`
# option, the users who join that group; they must already exist, and the
# install fails if one does not, so a wrong Feature order is never silent.
set -euo pipefail

# >>> vibesys network helpers (identical in every Feature; Features cannot share files)
# Every fetch in a Feature is bounded and retried a bounded number of times, so
# a stalled connection ends in a failure that names the step instead of hanging
# the image build until the caller's budget runs out. Only operations that are
# safe to repeat are retried: downloads to a path and package installs.
NET_ATTEMPTS=5
retry() {
    local attempt=1
    until "$@"; do
        if [ "${attempt}" -ge "${NET_ATTEMPTS}" ]; then
            echo "giving up after ${attempt} attempts: $*" >&2
            return 1
        fi
        echo "retry ${attempt}/${NET_ATTEMPTS} after failure: $*" >&2
        sleep $((attempt * 5))
        attempt=$((attempt + 1))
    done
}
# Fail a transfer that stalls (under 10 KB/s for 30 s) or runs over 10 minutes.
fetch() {
    retry curl -fsSL --connect-timeout 20 --speed-limit 10240 --speed-time 30 --max-time 600 "$@"
}
# Per-request cap. A transfer that keeps sending stays under it, so the update
# also has a total deadline (APT_UPDATE_DEADLINE).
APT_BOUNDS=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2)
# GPU bases ship NVIDIA CUDA or AMD ROCm lists. Their indexes are large, and a
# slow transfer never trips Acquire::http::Timeout, so one update can run until
# the CI job is cancelled. Features install only distribution packages.
APT_UPDATE_DEADLINE=60
drop_vendor_apt_sources() {
    local dir=/etc/apt/sources.list.d
    [ -d "${dir}" ] || return 0
    find "${dir}" -type f \( \
        -iname '*nvidia*' -o -iname '*cuda*' -o -iname '*rocm*' -o -iname '*amdgpu*' \
        -o -exec grep -Eqi 'nvidia\.com|repo\.radeon\.com|rocm|amdgpu' {} \; \
    \) -delete
}
apt_update_and_install() {
    drop_vendor_apt_sources
    timeout "${APT_UPDATE_DEADLINE}" apt-get update -qq "${APT_BOUNDS[@]}" &&
        apt-get install -y -qq --no-install-recommends "${APT_BOUNDS[@]}" "$@"
}
apt_install() {
    retry apt_update_and_install "$@"
}
# npm's own defaults (a 300 s request timeout, 2 retries) outlast a CI job's budget.
npm_install_global() {
    retry npm install -g --include=optional --fetch-retries=3 --fetch-timeout=60000 \
        --fetch-retry-mintimeout=5000 --fetch-retry-maxtimeout=30000 "$@"
}
pip_install() {
    retry pip install --no-cache-dir --retries 5 --timeout 30 "$@"
}
# <<< vibesys network helpers

for pair in \
    "docker_engine_version=${DOCKER_ENGINE_VERSION:-}" \
    "docker_compose_version=${DOCKER_COMPOSE_VERSION:-}" \
    "kind_version=${KIND_VERSION:-}" \
    "kubectl_version=${KUBECTL_VERSION:-}"; do
    if [ -z "${pair#*=}" ]; then
        echo "vibesys-container-runtime: option '${pair%%=*}' is required" >&2
        exit 1
    fi
done

arch="$(dpkg --print-architecture)"
case "${arch}" in
    amd64) docker_arch=x86_64; compose_arch=x86_64; go_arch=amd64 ;;
    arm64) docker_arch=aarch64; compose_arch=aarch64; go_arch=arm64 ;;
    *) echo "unsupported architecture for the container runtime: ${arch}" >&2; exit 1 ;;
esac

export DEBIAN_FRONTEND=noninteractive
apt_install curl ca-certificates tar iptables e2fsprogs pigz xz-utils procps kmod
rm -rf /var/lib/apt/lists/*

fetch -o /tmp/docker.tgz \
    "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_ENGINE_VERSION}.tgz"
tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1
rm -f /tmp/docker.tgz
mkdir -p /usr/local/lib/docker/cli-plugins
fetch \
    -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${compose_arch}"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fetch -o /usr/local/bin/kind \
    "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${go_arch}"
fetch -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${go_arch}/kubectl"
chmod +x /usr/local/bin/kind /usr/local/bin/kubectl
docker --version
docker compose version
kind version
kubectl version --client

groupadd --force docker
for user in ${USERS:-}; do
    if ! id -u "${user}" >/dev/null 2>&1; then
        echo "vibesys-container-runtime: user '${user}' does not exist; install the Feature that creates it first" >&2
        exit 1
    fi
    usermod -aG docker "${user}"
done
