#!/usr/bin/env bash
# Re-vendor the NKI skills from aws-neuron/neuron-agentic-development (Apache-2.0).
# Usage: ./update.sh [git-ref]   (default: the pinned commit below)
set -euo pipefail

REPO="https://github.com/aws-neuron/neuron-agentic-development.git"
PINNED="648923a2065f61b771dd8a2386236dae846078cc"
REF="${1:-$PINNED}"
SKILLS=(neuron-nki-writing neuron-nki-docs neuron-nki-debugging \
        neuron-nki-profiling neuron-nki-profile-querying)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone "$REPO" "$TMP/nad"
git -C "$TMP/nad" checkout "$REF"

rm -rf "$HERE/skills"
mkdir -p "$HERE/skills"
for s in "${SKILLS[@]}"; do
  cp -r "$TMP/nad/skills/$s" "$HERE/skills/$s"
done
cp "$TMP/nad/LICENSE.txt" "$HERE/LICENSE.txt"
cp "$TMP/nad/NOTICE" "$HERE/NOTICE"

echo "Re-vendored ${#SKILLS[@]} NKI skills from $REPO @ $(git -C "$TMP/nad" rev-parse HEAD)"
echo "Remember to update the pinned commit in README.md and this script."
