#!/usr/bin/env bash
# The Go toolchain as a Dev Container Feature. Self-contained: installs the
# packages it needs (curl and tar to fetch the release) and depends on no other
# Feature.
set -euo pipefail

if [ -z "${GO_VERSION:-}" ]; then
    echo "vibesys-go: option 'go_version' is required" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
installed=""
for attempt in 1 2 3 4 5; do
    if apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        curl ca-certificates tar; then
        installed=1
        break
    fi
    echo "apt retry ${attempt}..." >&2
    sleep $((attempt * 5))
done
[ -n "${installed}" ]
rm -rf /var/lib/apt/lists/*

arch="$(dpkg --print-architecture)"
case "${arch}" in
    amd64) go_arch=amd64 ;;
    arm64) go_arch=arm64 ;;
    *) echo "unsupported architecture for Go: ${arch}" >&2; exit 1 ;;
esac
curl -fsSL --retry 5 --retry-delay 5 -o /tmp/go.tar.gz \
    "https://go.dev/dl/go${GO_VERSION}.linux-${go_arch}.tar.gz"
rm -rf /usr/local/go
tar -xzf /tmp/go.tar.gz -C /usr/local
rm -f /tmp/go.tar.gz
chmod -R a+rX /usr/local/go
/usr/local/go/bin/go version

# Debian's /etc/profile resets PATH for login shells (`bash -lc`); re-add the
# toolchain so a login shell and a plain `docker exec` agree on PATH.
echo 'export PATH="/usr/local/go/bin:${PATH}"' > /etc/profile.d/vibesys-go.sh
