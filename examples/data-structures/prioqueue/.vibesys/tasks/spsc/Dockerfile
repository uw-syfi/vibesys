# Execution environment for a Rust-candidate concurrent data-structure task:
# the candidate builds with `cargo`/`make`, and the trusted evaluator package
# runs with `go` (cgo needs the C toolchain). Nothing from the candidate or the
# task is copied in; VibeSys mounts the project at run time.
#
# The Rust and Go versions equal the pins VibeSys layers onto every run image
# (`RUST_TOOLCHAIN_VERSION` and `GO_TOOLCHAIN_VERSION` in
# `libs/vs-sandbox/src/vs_sandbox/toolchain_features.py`), so the agent layer
# reinstalls the same versions instead of adding a second toolchain. The
# architecture is the build's (`TARGETARCH`).
# `libs/vs-sandbox/tests/test_example_task_dockerfiles.py` keeps the pins in
# step, and keeps all 17 Rust + Go Dockerfiles identical: edit them together.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3

ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256_X86_64=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUSTUP_SHA256_AARCH64=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
ARG RUST_TOOLCHAIN=1.92.0
ARG GO_VERSION=1.23.12
ARG GO_SHA256_AMD64=d3847fef834e9db11bf64e3fb34db9c04db14e068eeb064f49af747010454f90
ARG GO_SHA256_ARM64=52ce172f96e21da53b1ae9079808560d49b02ac86cecfa457217597f9bc28ab3
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${arch}" in \
        amd64) triple=x86_64-unknown-linux-gnu; sha256="${RUSTUP_SHA256_X86_64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu; sha256="${RUSTUP_SHA256_AARCH64}" ;; \
        *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
        "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${triple}/rustup-init" \
        --output /tmp/rustup-init \
    && echo "${sha256}  /tmp/rustup-init" | sha256sum --check --strict \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init --no-modify-path --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" -y \
    && mkdir -p /opt/cargo/registry /opt/cargo/git \
    && chmod -R a+rX /opt/rustup \
    && chmod -R a+rwX /opt/cargo \
    && rm /tmp/rustup-init

RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${arch}" in \
        amd64) sha256="${GO_SHA256_AMD64}" ;; \
        arm64) sha256="${GO_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
        "https://go.dev/dl/go${GO_VERSION}.linux-${arch}.tar.gz" --output /tmp/go.tar.gz \
    && echo "${sha256}  /tmp/go.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/go.tar.gz -C /usr/local \
    && rm /tmp/go.tar.gz \
    && chmod -R a+rX /usr/local/go

# CARGO_HOME is writable by any user: the trusted evaluator builds its own
# runner with cargo (which fetches crates) as whichever user the run maps in.
ENV PATH=/opt/cargo/bin:/usr/local/go/bin:${PATH}
WORKDIR /workspace

RUN cargo --version && rustc --version && go version && make --version | head -n 1
