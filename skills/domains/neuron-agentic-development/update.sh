#!/usr/bin/env bash
# Re-vendor the NKI skills from aws-neuron/neuron-agentic-development (Apache-2.0).
# Usage: ./update.sh [git-ref]   (default: the pinned commit below)
set -euo pipefail

REPO="https://github.com/aws-neuron/neuron-agentic-development.git"
PINNED="e63853627271bcb98d941c242641e698d210d5a0"
REF="${1:-$PINNED}"
SKILLS=(neuron-nki-writing neuron-nki-docs neuron-nki-debugging \
        neuron-nki-profiling neuron-nki-profile-querying \
        neuron-explorer-profile-schema)

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
# Fix-up: upstream's device-harness snippets still use the retired torch_xla
# device. VibeSys targets native TorchNeuron, so point readers at the native
# harness right after the frontmatter instead of rewriting upstream code.
NATIVE_NOTE='> **VibeSys note (native TorchNeuron):** the harness snippets below use the retired `torch_xla` device (`xm.xla_device()`). In this container call `@nki.jit` kernels directly on `torch.device("neuron")` tensors (no `torch_xla`), or wrap them with `torch_neuronx.nki_op`; see the serving-systems `neuron-pytorch` reference. The compiler-error workflow is unchanged.'
for f in "$HERE/skills/neuron-nki-debugging/SKILL.md"; do
  awk -v note="$NATIVE_NOTE" '{ print } /^---$/ && ++n == 2 { print ""; print note }' "$f" >"$f.tmp"
  mv "$f.tmp" "$f"
done

cp "$TMP/nad/LICENSE.txt" "$HERE/LICENSE.txt"
cp "$TMP/nad/NOTICE" "$HERE/NOTICE"

echo "Re-vendored ${#SKILLS[@]} Neuron skills from $REPO @ $(git -C "$TMP/nad" rev-parse HEAD)"
echo "Remember to update the pinned commit in README.md and this script."
