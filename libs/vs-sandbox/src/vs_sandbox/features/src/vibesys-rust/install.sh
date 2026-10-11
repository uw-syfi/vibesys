#!/usr/bin/env bash
# The Rust toolchain as a Dev Container Feature. Self-contained: installs the packages it needs (curl to
# fetch rustup, a C linker for cargo) and depends on no other Feature.
set -euo pipefail

if [ -z "${RUST_VERSION:-}" ]; then
    echo "vibesys-rust: option 'rust_version' is required" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
installed=""
for attempt in 1 2 3 4 5; do
    if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates build-essential; then
        installed=1
        break
    fi
    echo "apt retry ${attempt}..." >&2
    sleep $((attempt * 5))
done
[ -n "${installed}" ]
rm -rf /var/lib/apt/lists/*

export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
curl -fsSL --retry 5 --retry-delay 5 -o /tmp/rustup-init.sh https://sh.rustup.rs
sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}"
rm -f /tmp/rustup-init.sh
chmod -R a+rX /opt/rustup /opt/cargo

# Debian's /etc/profile resets PATH for login shells (`bash -lc`); re-add the
# toolchain so a login shell and a plain `docker exec` agree on PATH.
echo 'export PATH="/opt/cargo/bin:${PATH}"' > /etc/profile.d/vibesys-rust.sh
