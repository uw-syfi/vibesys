#!/usr/bin/env bash
set -e
source dev-container-features-test-lib
check "cargo runs" cargo --version
check "rustc runs" rustc --version
check "login shell finds cargo" bash -lc 'cargo --version'
check "toolchain readable by nobody" su -s /bin/sh nobody -c 'RUSTUP_HOME=/opt/rustup /opt/cargo/bin/cargo --version'
reportResults
