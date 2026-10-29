#!/usr/bin/env bash
# Installs an opt-in git pre-push hook. On every push it runs scripts/check_private_terms.sh
# (a no-op without your private terms list); when the branch being pushed changes anything
# under clients/ it also runs scripts/check_clients.sh. Nothing installs it for you: run this
# once per clone, and again to pick up a changed hook.
# `git push --no-verify` skips it for one push. It refuses to replace a hook it did not write.
#
#   ./scripts/install_pre_push_hook.sh            # install
#   ./scripts/install_pre_push_hook.sh --remove   # remove it again
set -euo pipefail

marker="# vibesys-client-checks-pre-push"
hook="$(git rev-parse --git-path hooks/pre-push)"

if [[ "${1:-}" == "--remove" ]]; then
  if [[ -f "$hook" ]] && grep -q "$marker" "$hook"; then
    rm "$hook"
    echo "removed $hook"
  else
    echo "no hook of ours at $hook"
  fi
  exit 0
fi

if [[ -e "$hook" ]] && ! grep -q "$marker" "$hook"; then
  echo "$hook already exists and is not ours; remove or merge it by hand" >&2
  exit 1
fi

mkdir -p "$(dirname "$hook")"
cat >"$hook" <<HOOK
#!/usr/bin/env bash
$marker
set -euo pipefail
root="\$(git rev-parse --show-toplevel)"
cd "\$root"
./scripts/check_private_terms.sh
if git diff --quiet --exit-code origin/main...HEAD -- clients/ 2>/dev/null; then
  exit 0
fi
exec ./scripts/check_clients.sh
HOOK
chmod +x "$hook"
echo "installed $hook"
