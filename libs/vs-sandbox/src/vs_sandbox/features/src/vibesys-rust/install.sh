#!/usr/bin/env bash
# The Rust toolchain as a Dev Container Feature. Self-contained: installs the packages it needs (curl to
# fetch rustup, a C linker for cargo) and depends on no other Feature.
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

if [ -z "${RUST_VERSION:-}" ]; then
    echo "vibesys-rust: option 'rust_version' is required" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt_install curl ca-certificates build-essential
rm -rf /var/lib/apt/lists/*

export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
fetch -o /tmp/rustup-init.sh https://sh.rustup.rs
retry timeout 600 sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain "${RUST_VERSION}"
rm -f /tmp/rustup-init.sh
chmod -R a+rX /opt/rustup /opt/cargo

# Debian's /etc/profile resets PATH for login shells (`bash -lc`); re-add the
# toolchain so a login shell and a plain `docker exec` agree on PATH.
echo 'export PATH="/opt/cargo/bin:${PATH}"' > /etc/profile.d/vibesys-rust.sh
