#!/usr/bin/env bash
set -e
source dev-container-features-test-lib
check "go runs" go version
check "login shell finds go" bash -lc 'go version'
check "toolchain readable by nobody" su -s /bin/sh nobody -c '/usr/local/go/bin/go version'
reportResults
