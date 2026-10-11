# The devcontainer CLI in a container, for hosts without Node.js. It drives the
# host's Docker daemon through a mounted /var/run/docker.sock. Spike artifact
# for the CLI-invocation decision in docs/contributing/devcontainer-features.md.
FROM node:24.21.0-bookworm-slim
ARG DEVCONTAINERS_CLI_VERSION=0.89.0
ARG DOCKER_CLI_VERSION=27.5.1
ARG BUILDX_VERSION=0.30.1
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
       "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-amd64" \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
RUN npm install -g "@devcontainers/cli@${DEVCONTAINERS_CLI_VERSION}"
ENTRYPOINT ["devcontainer"]
