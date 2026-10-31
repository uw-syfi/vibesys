# The devcontainer CLI in a container, for authoring and CI only: test_features.sh
# runs `devcontainer features test` with it. It drives the host's Docker daemon
# through a mounted /var/run/docker.sock, which a hardened host may forbid, so a
# run never uses it (runs build images with plain `docker build`). See
# docs/contributing/devcontainer-features.md.
# test_features.sh passes the CI mirror of this pin when it is readable; the default is the same digest upstream.
ARG BASE_IMAGE=docker.io/library/node@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
FROM ${BASE_IMAGE}
ARG DEVCONTAINERS_CLI_VERSION=0.89.0
ARG DOCKER_CLI_VERSION=27.5.1
ARG BUILDX_VERSION=0.30.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) docker_arch=x86_64 ;; \
        arm64) docker_arch=aarch64 ;; \
        *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends curl ca-certificates git; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
        "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-${arch}"; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
RUN npm install -g "@devcontainers/cli@${DEVCONTAINERS_CLI_VERSION}"
ENTRYPOINT ["devcontainer"]
