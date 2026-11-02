# Execution environment for the differential-dataflow CPU task. The engine is
# cloned into the candidate workspace at run time (see `[[workspace.sources]]` in
# vibesys.input.toml), and every cargo build the checker and benchmark run is
# `--offline`, so this image carries what they need and nothing else:
#   - stable Rust for the build and benchmark;
#   - a pinned nightly with `rust-src` for the ThreadSanitizer gate
#     (`cargo +nightly build -Zbuild-std`);
#   - `valgrind` and `callgrind_annotate` for the profiler;
#   - the engine's crates.io dependencies, and the standard library's own
#     (for `-Zbuild-std`), in CARGO_HOME, so no build downloads anything.
# The dependency warm-up clones the same pinned commit the task materializes
# (`ENGINE_COMMIT` equals the commit in vibesys.input.toml, and the Rust versions
# equal the catalog's `RUST_TOOLCHAIN_VERSION`; `libs/vs-sandbox/tests/test_example_task_dockerfiles.py` holds both).
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256_X86_64=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUSTUP_SHA256_AARCH64=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
ARG RUST_TOOLCHAIN=1.92.0
ARG RUST_NIGHTLY=nightly-2025-11-15
ARG ENGINE_REPO=https://github.com/HQingXuan/differential-dataflow-pinned
ARG ENGINE_COMMIT=4f05cbb61775a45844a0905de9dacfee1e91dd80
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates curl git libc6-dbg perl valgrind=1:3.19.* \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /uvx /usr/local/bin/

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
    && rm /tmp/rustup-init \
    && RUSTUP_IO_THREADS=1 /opt/cargo/bin/rustup toolchain install "${RUST_NIGHTLY}" --no-self-update --profile minimal --component rust-src \
    # The gate asks for the toolchain by the channel name `nightly`; the dated
    # install above is what makes that name reproducible.
    && ln -s "${RUST_NIGHTLY}-${triple}" "/opt/rustup/toolchains/nightly-${triple}"

ENV PATH=/opt/cargo/bin:${PATH}

# Warm CARGO_HOME: the engine's locked dependencies, then the standard library's.
RUN git clone --quiet "${ENGINE_REPO}" /tmp/engine \
    && git -C /tmp/engine checkout --quiet "${ENGINE_COMMIT}" \
    && cargo fetch --locked --manifest-path /tmp/engine/Cargo.toml \
    && cargo +nightly fetch --locked \
        --manifest-path "$(rustc +nightly --print sysroot)/lib/rustlib/src/rust/library/Cargo.toml" \
    && rm -rf /tmp/engine \
    && chmod -R a+rX /opt/rustup \
    && chmod -R a+rwX /opt/cargo

WORKDIR /workspace

RUN python --version && uv --version && cargo --version && cargo +nightly --version \
    && valgrind --version && command -v callgrind_annotate
