# Execution environment for the Python key-value store task: `uv run python`
# drives the checker and benchmark, and the checker runs its vendored Porcupine
# linearizability checker with `go run -mod=vendor`. Nothing from the task or
# the candidate is copied in; VibeSys mounts the project at run time.
#
# The Go version equals the pin VibeSys layers onto every run image
# (`GO_TOOLCHAIN_VERSION` in `libs/vs-sandbox/src/vs_sandbox/toolchain_features.py`).
# Keep them in step.
FROM --platform=linux/amd64 python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG GO_VERSION=1.23.12
ARG GO_SHA256=d3847fef834e9db11bf64e3fb34db9c04db14e068eeb064f49af747010454f90

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /uvx /usr/local/bin/

RUN curl --fail --location --silent --show-error \
        "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" --output /tmp/go.tar.gz \
    && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/go.tar.gz -C /usr/local \
    && rm /tmp/go.tar.gz \
    && chmod -R a+rX /usr/local/go

ENV PATH=/usr/local/go/bin:${PATH}
WORKDIR /workspace

RUN python --version && uv --version && go version
