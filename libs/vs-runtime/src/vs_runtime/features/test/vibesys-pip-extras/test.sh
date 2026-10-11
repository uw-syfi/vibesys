#!/usr/bin/env bash
set -e
source dev-container-features-test-lib
check "modal client imports" bash -c 'python3 -c "import modal" || python -c "import modal"'
check "pip finds the modal client" bash -c 'pip show modal'
reportResults
