#!/usr/bin/env bash
# The container-topology tools as a Dev Container Feature. Container-topology
# tasks (compose or kind candidates) run their own Docker daemon inside the
# sandbox container, started at container start by `vs_sandbox` under the
# Sysbox runtime. Only the tools land here: static engine binaries (no apt
# repository key to manage), the compose plugin, kind, and kubectl, plus the
# `docker` group that owns the daemon socket. Which user joins that group is
# decided by the Feature that creates the user (the agent layer), so neither
# Feature depends on the other's install order.
set -euo pipefail

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
installed=""
for attempt in 1 2 3 4 5; do
    if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates tar iptables e2fsprogs pigz xz-utils procps kmod; then
        installed=1
        break
    fi
    echo "apt retry ${attempt}..." >&2
    sleep $((attempt * 5))
done
[ -n "${installed}" ]
rm -rf /var/lib/apt/lists/*

curl -fsSL --retry 5 --retry-delay 5 -o /tmp/docker.tgz \
    "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_ENGINE_VERSION}.tgz"
tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1
rm -f /tmp/docker.tgz
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL --retry 5 --retry-delay 5 \
    -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${compose_arch}"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
curl -fsSL --retry 5 --retry-delay 5 -o /usr/local/bin/kind \
    "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${go_arch}"
curl -fsSL --retry 5 --retry-delay 5 -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${go_arch}/kubectl"
chmod +x /usr/local/bin/kind /usr/local/bin/kubectl
docker --version
docker compose version
kind version
kubectl version --client

groupadd --force docker
