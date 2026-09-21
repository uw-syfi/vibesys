ARG VERUS_PLATFORM=linux/amd64
FROM --platform=${VERUS_PLATFORM} ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

ARG VERUS_VERSION=0.2026.08.30.b432e82
ARG VERUS_SHA256=067f5f72a457fe66b77c0c10b180f2a919a9c7481a8baa024ffc716aa931a41b
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.97.1-x86_64-unknown-linux-gnu

ENV DEBIAN_FRONTEND=noninteractive
ENV RUSTUP_HOME=/opt/rustup
ENV CARGO_HOME=/opt/cargo
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates curl git python3 unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --location --silent --show-error \
        "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
        --output /tmp/rustup-init \
    && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum --check --strict \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init --no-modify-path --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" -y \
    && chmod -R a+rX /opt/rustup /opt/cargo \
    && rm /tmp/rustup-init

RUN curl --fail --location --silent --show-error \
        "https://github.com/verus-lang/verus/releases/download/release%2F${VERUS_VERSION}/verus-${VERUS_VERSION}-x86-linux.zip" \
        --output /tmp/verus.zip \
    && echo "${VERUS_SHA256}  /tmp/verus.zip" | sha256sum --check --strict \
    && unzip -q /tmp/verus.zip -d /opt \
    && mv /opt/verus-x86-linux /opt/verus \
    && rm /tmp/verus.zip

# Codex supplies its launcher PATH to agent shells. Put the task toolchain on a
# conventional path so both agents and framework gates resolve the same tools.
RUN ln -s /opt/cargo/bin/cargo /usr/local/bin/cargo \
    && ln -s /opt/cargo/bin/rustc /usr/local/bin/rustc \
    && ln -s /opt/cargo/bin/rustup /usr/local/bin/rustup \
    && ln -s /opt/verus/cargo-verus /usr/local/bin/cargo-verus \
    && ln -s /opt/verus/verus /usr/local/bin/verus

ENV PATH=/opt/verus:/opt/cargo/bin:${PATH}
WORKDIR /workspace

RUN verus --version && cargo-verus --help >/dev/null && rustc --version
