# Agent layer, built on top of a task image (or a backend base image when the
# task has no Dockerfile of its own). Installs the shipped CLIs, helper
# tools, and a non-root `agent` user; ends with `USER agent` so a container
# never runs a provider CLI as root. Built by `vs_agent.api.images.agent_image`,
# which supplies every ARG below from `vs_agent.provider_policy`.
#
# Debian/Ubuntu only: the apt step below requires it. All current backend
# bases and task Dockerfiles are.

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG NODE_VERSION
ARG CLAUDE_VERSION
ARG CODEX_VERSION
ARG GEMINI_VERSION
ARG OPENCODE_VERSION
# The interpreter of every VibeSys-owned process in the container (its tool
# servers). Independent of the base image's `python`, which may be a conda
# Python 3.10 or absent.
ARG PYTHON_VERSION
ARG PYTHON_HOME
# Space-separated subset of "rust go container-runtime". Empty skips all of them.
ARG TOOLCHAINS=""
ARG RUST_VERSION
ARG GO_VERSION
# Versions of the "container-runtime" toolchain: a Docker engine (dockerd and
# the docker CLI), the compose plugin, kind, and kubectl.
ARG DOCKER_ENGINE_VERSION
ARG DOCKER_COMPOSE_VERSION
ARG KIND_VERSION
ARG KUBECTL_VERSION
# Space-separated extra pip requirements an execution environment needs in
# the editor container (the Modal environment adds the `modal` client so a
# candidate's `modal run` works). Empty installs nothing extra.
ARG PIP_EXTRAS=""

ENV DEBIAN_FRONTEND=noninteractive

# Retrying because archive mirrors reachable from our hosts return transient
# fetch failures often enough to abort a single-shot `apt-get update`.
# Idempotent: re-running against packages already installed is a no-op, and
# Docker's own layer cache means this step only actually runs once per
# unchanged base image.
RUN set -eux; \
    installed=""; \
    for attempt in 1 2 3 4 5; do \
        if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
            curl ca-certificates git ripgrep python3 python3-pip python3-venv \
            tar unzip build-essential; then \
            installed=1; \
            break; \
        fi; \
        echo "apt retry ${attempt}..." >&2; \
        sleep $((attempt * 5)); \
    done; \
    [ -n "${installed}" ]; \
    rm -rf /var/lib/apt/lists/*

# Node from the nodejs.org tarball, pinned, extracted straight into
# /usr/local (its bin/lib/share/include layout merges with what's already
# there). apt-get install nodejs is not used: the version in Debian/Ubuntu
# archives lags far behind the CLIs below and archive mirrors are the least
# reliable network path from our hosts.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported architecture for Node: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 --retry-delay 5 -o /tmp/node.tar.gz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz"; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1; \
    rm -f /tmp/node.tar.gz; \
    node --version; \
    npm --version

# All four shipped CLIs in one layer: the provider a session runs is a
# run-time choice, not something baked into the image.
# --include=optional: codex ships its Linux-x64 native binary as an optional
# dependency that `npm install -g` skips under some npm configurations.
RUN npm install -g --include=optional \
        @anthropic-ai/claude-code@${CLAUDE_VERSION} \
        @openai/codex@${CODEX_VERSION} \
        @google/gemini-cli@${GEMINI_VERSION} \
        opencode-ai@${OPENCODE_VERSION}

# Debian 12+ marks the system Python as externally managed (PEP 668); this
# image has no other consumer of that protection.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip install --no-cache-dir uv ${PIP_EXTRAS}

# VibeSys's own interpreter: a uv-managed Python (never the base image's) in a
# virtual environment at a fixed path, holding the MCP library (and so
# pydantic) the framework's tool servers import. The framework's own packages
# arrive on PYTHONPATH from the read-only framework mount at run time.
RUN set -eux; \
    UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv python install --no-cache "${PYTHON_VERSION}"; \
    UV_PYTHON_INSTALL_DIR=/opt/vibesys-python-install uv venv --no-cache \
        --python "${PYTHON_VERSION}" --python-preference only-managed "${PYTHON_HOME}"; \
    uv pip install --no-cache --python "${PYTHON_HOME}/bin/python" 'mcp>=1.0,<2'; \
    chmod -R a+rX /opt/vibesys-python-install "${PYTHON_HOME}"; \
    "${PYTHON_HOME}/bin/python" -c 'import mcp, pydantic, sys; print(sys.version)'

# Harmless when the corresponding toolchain is absent, so these are set
# unconditionally rather than only inside the TOOLCHAINS branches below.
ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo
ENV PATH="/opt/cargo/bin:/usr/local/go/bin:${PATH}"

# `ENV PATH` only reaches a process that inherits the image's environment.
# Debian's /etc/profile resets PATH from scratch for a *login* shell (`bash
# -l`, or `bash -lc "..."`) before sourcing /etc/profile.d/*.sh, which would
# otherwise silently drop the toolchain directories above for exactly that
# invocation style. Re-adding them here keeps a login shell and a plain
# `docker exec` agreeing on PATH.
RUN echo 'export PATH="/opt/cargo/bin:/usr/local/go/bin:${PATH}"' \
        > /etc/profile.d/vibesys-agent-toolchains.sh

# Toolchains are opt-in per task and installed at build time so agents never
# need to bootstrap one mid-round. Mirrors the rustup invocation task
# Dockerfiles already use (see
# examples/data-structures/repositories/queue-rs/.vibesys/tasks/verus-mpmc-open/Dockerfile).
RUN set -eux; \
    case " ${TOOLCHAINS} " in \
        *" rust "*) \
            curl -fsSL --retry 5 --retry-delay 5 -o /tmp/rustup-init.sh https://sh.rustup.rs; \
            sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}"; \
            rm -f /tmp/rustup-init.sh; \
            chmod -R a+rX /opt/rustup /opt/cargo; \
            ;; \
        *) ;; \
    esac

RUN set -eux; \
    case " ${TOOLCHAINS} " in \
        *" go "*) \
            arch="$(dpkg --print-architecture)"; \
            case "${arch}" in \
                amd64) go_arch=amd64 ;; \
                arm64) go_arch=arm64 ;; \
                *) echo "unsupported architecture for Go: ${arch}" >&2; exit 1 ;; \
            esac; \
            curl -fsSL --retry 5 --retry-delay 5 -o /tmp/go.tar.gz \
                "https://go.dev/dl/go${GO_VERSION}.linux-${go_arch}.tar.gz"; \
            rm -rf /usr/local/go; \
            tar -xzf /tmp/go.tar.gz -C /usr/local; \
            rm -f /tmp/go.tar.gz; \
            chmod -R a+rX /usr/local/go; \
            ;; \
        *) ;; \
    esac

# Container-topology tasks (compose or kind candidates) run their own Docker
# daemon inside the sandbox container, started at container start by
# `vs_sandbox` under the Sysbox runtime. Only the tools land here: static
# engine binaries (no apt repository key to manage), the compose plugin, kind,
# and kubectl. Opt-in like the language toolchains, so ordinary tasks' images
# do not grow. The `docker` group owns the daemon socket; the agent joins it
# below, once the agent user exists.
RUN set -eux; \
    case " ${TOOLCHAINS} " in \
        *" container-runtime "*) \
            arch="$(dpkg --print-architecture)"; \
            case "${arch}" in \
                amd64) docker_arch=x86_64; compose_arch=x86_64; go_arch=amd64 ;; \
                arm64) docker_arch=aarch64; compose_arch=aarch64; go_arch=arm64 ;; \
                *) echo "unsupported architecture for the container runtime: ${arch}" >&2; exit 1 ;; \
            esac; \
            apt-get update -qq; \
            apt-get install -y -qq --no-install-recommends \
                iptables e2fsprogs pigz xz-utils procps kmod; \
            rm -rf /var/lib/apt/lists/*; \
            curl -fsSL --retry 5 --retry-delay 5 -o /tmp/docker.tgz \
                "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_ENGINE_VERSION}.tgz"; \
            tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1; \
            rm -f /tmp/docker.tgz; \
            mkdir -p /usr/local/lib/docker/cli-plugins; \
            curl -fsSL --retry 5 --retry-delay 5 \
                -o /usr/local/lib/docker/cli-plugins/docker-compose \
                "https://github.com/docker/compose/releases/download/v${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${compose_arch}"; \
            chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
            curl -fsSL --retry 5 --retry-delay 5 -o /usr/local/bin/kind \
                "https://kind.sigs.k8s.io/dl/v${KIND_VERSION}/kind-linux-${go_arch}"; \
            curl -fsSL --retry 5 --retry-delay 5 -o /usr/local/bin/kubectl \
                "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${go_arch}/kubectl"; \
            chmod +x /usr/local/bin/kind /usr/local/bin/kubectl; \
            docker --version; \
            docker compose version; \
            kind version; \
            kubectl version --client; \
            groupadd --force docker; \
            ;; \
        *) ;; \
    esac

# The sandbox remaps this uid to the host uid at container start, so what
# matters here is the fixed uid (1000), not the name. Reuse a base image's
# existing uid-1000 account by renaming it instead of failing on a collision.
# Its primary group is renamed too: the sandbox runs `chown agent:agent`, and
# renaming only the user leaves the base's group (`jenkins` in the ROCm
# PyTorch image) under its old name.
RUN set -eux; \
    if id -u agent >/dev/null 2>&1; then \
        : ; \
    elif getent passwd 1000 >/dev/null 2>&1; then \
        existing="$(getent passwd 1000 | cut -d: -f1)"; \
        usermod --login agent --home /home/agent --move-home --shell /bin/bash "${existing}"; \
    else \
        useradd --create-home --uid 1000 --shell /bin/bash agent; \
    fi; \
    gid="$(id -g agent)"; \
    if ! getent group agent >/dev/null 2>&1; then \
        if getent group "${gid}" >/dev/null 2>&1; then \
            groupmod --new-name agent "$(getent group "${gid}" | cut -d: -f1)"; \
        else \
            groupadd --gid "${gid}" agent; \
        fi; \
    fi

RUN set -eux; \
    case " ${TOOLCHAINS} " in \
        *" container-runtime "*) usermod -aG docker agent ;; \
        *) ;; \
    esac

RUN mkdir -p /workspace && chown agent /workspace

WORKDIR /workspace
USER agent
ENV HOME=/home/agent
