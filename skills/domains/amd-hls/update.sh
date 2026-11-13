#!/usr/bin/env bash
# Re-vendor the Vitis HLS design-guidance skills from Xilinx/ross-ai-assistant (MIT).
# Usage: ./update.sh [git-ref]   (default: the pinned commit below)
set -euo pipefail

REPO="https://github.com/Xilinx/ross-ai-assistant.git"
PINNED="dce0fb90611b144dd529490359b51cea5d0d5596"
REF="${1:-$PINNED}"
# Design-guidance skills only. The flow-running, report-reading and project-setup
# skills (hls-run-flow, hls-*-report, hls-component-basic-info, hls-optimize,
# hls-architect) are omitted: in VibeSys the evaluator owns every Vitis flow.
SKILLS=(hls-pragma-scope hls-synthesizable hls-pipeline hls-ii-diagnose \
        hls-dataflow hls-array-to-stream hls-burst-inference hls-flattenable)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone "$REPO" "$TMP/ross"
git -C "$TMP/ross" checkout "$REF"

rm -rf "$HERE/skills"
mkdir -p "$HERE/skills"
for s in "${SKILLS[@]}"; do
  cp -RL "$TMP/ross/skills/$s" "$HERE/skills/$s"
done
# Fix-up: upstream skills assume AMD's IDE workflow, where the agent runs Vitis
# flows and reads reports through companion skills that are not vendored here.
# Add one note after the frontmatter instead of rewriting upstream text.
VIBESYS_NOTE='> **VibeSys note:** in a VibeSys run the task evaluator runs every Vitis flow (C simulation, synthesis, co-simulation, implementation) and returns its reports as evaluation evidence. Take the top function from the task'"'"'s candidate contract. The skills this text names for running flows or reading reports (`hls-run-flow`, `hls-synth-report`, `hls-component-basic-info`, `hls-optimize`, `hls-loop-unrollable`) are not installed; use the evaluator'"'"'s reports and the task contract instead.'
for f in "$HERE"/skills/*/SKILL.md; do
  awk -v note="$VIBESYS_NOTE" '{ print } /^---$/ && ++n == 2 { print ""; print note }' "$f" >"$f.tmp"
  mv "$f.tmp" "$f"
done

cp "$TMP/ross/LICENSE" "$HERE/LICENSE"

echo "Re-vendored ${#SKILLS[@]} HLS skills from $REPO @ $(git -C "$TMP/ross" rev-parse HEAD)"
echo "Remember to update the pinned commit in README.md and this script."
