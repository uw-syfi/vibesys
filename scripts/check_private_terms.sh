#!/usr/bin/env bash
# Fails when tracked, staged or outgoing content names a term from a private list kept outside
# the repository: $VIBESYS_PRIVATE_TERMS, default ~/.config/vibesys/private-terms.txt, one
# case-insensitive regex per line. A missing list passes with a one-line note. Findings are
# file:line and never echo the matched text. The logic and its tests are in
# check_private_terms.py; the tracked pre-push hook (.githooks/pre-push) runs this on
# every push. CI cannot run it: the terms are private by design.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_private_terms.py" "$@"
