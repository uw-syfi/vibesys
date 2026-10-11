#!/usr/bin/env bash
set -e
source dev-container-features-test-lib
check "docker CLI runs" docker --version
check "dockerd is installed" dockerd --version
check "compose plugin runs" docker compose version
check "kind runs" kind version
check "kubectl runs" kubectl version --client
check "docker group exists" getent group docker
reportResults
