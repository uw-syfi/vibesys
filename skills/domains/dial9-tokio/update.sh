#!/usr/bin/env bash
# Re-vendor the dial9 Tokio-tracing skills from dial9-rs/dial9 (Apache-2.0).
# Usage: ./update.sh [git-ref]   (default: the pinned commit below)
set -euo pipefail

REPO="https://github.com/dial9-rs/dial9.git"
PINNED="ab680e74f9a1d05216512a83f42ad40701c07232"
REF="${1:-$PINNED}"
SKILLS=(dial9-toolkit dial9-trace-loading dial9-trace-analysis \
        dial9-trace-recipes dial9-red-flags dial9-runtime \
        dial9-diagnose-long-poll dial9-zoom-window)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone "$REPO" "$TMP/dial9"
git -C "$TMP/dial9" checkout "$REF"

rm -rf "$HERE/skills"
mkdir -p "$HERE/skills"
for s in "${SKILLS[@]}"; do
  # -L, not -r alone: the skill trees symlink the shared toolkit libraries and
  # the viewer URL contract out to dial9-viewer/ui/, which is not vendored, so a
  # link-preserving copy would leave dangling links and a broken toolkit.
  cp -rL "$TMP/dial9/dial9-viewer/skills/$s" "$HERE/skills/$s"
done
cp "$TMP/dial9/LICENSE" "$HERE/LICENSE.txt"
# Upstream ships no NOTICE, so `$HERE/NOTICE` is ours and is left in place.

if [ -n "$(find "$HERE/skills" -type l -print -quit)" ]; then
  echo "error: symlinks survived the copy; the toolkit libraries will not resolve" >&2
  find "$HERE/skills" -type l >&2
  exit 1
fi

# Upstream writes `description:` as a plain YAML scalar, and some descriptions
# contain ": ", which agent harnesses tolerate but a strict YAML parser (what
# VibeSys validates frontmatter with) rejects. Quote the scalar here rather than
# hand-editing the vendored tree, so the fix survives the next re-vendor, and
# fail loudly if any frontmatter is still invalid afterwards.
python3 - "$HERE/skills" <<'PY'
import json
import pathlib
import sys

import yaml

DELIMITER = "---"

for skill_md in sorted(pathlib.Path(sys.argv[1]).rglob("SKILL.md")):
    lines = skill_md.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIMITER:
        sys.exit(f"{skill_md}: missing opening YAML frontmatter delimiter")
    closing = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() == DELIMITER), None
    )
    if closing is None:
        sys.exit(f"{skill_md}: missing closing YAML frontmatter delimiter")

    front = lines[1:closing]
    try:
        yaml.safe_load("".join(front))
        continue
    except yaml.YAMLError:
        pass

    quoted = []
    for line in front:
        value = line.partition("description:")
        if value[1] and not value[0].strip() and not value[2].strip().startswith(('"', "'")):
            line = f"description: {json.dumps(value[2].strip(), ensure_ascii=False)}\n"
        quoted.append(line)
    try:
        yaml.safe_load("".join(quoted))
    except yaml.YAMLError as exc:
        sys.exit(f"{skill_md}: frontmatter is still invalid YAML after quoting: {exc}")
    skill_md.write_text("".join(lines[:1] + quoted + lines[closing:]), encoding="utf-8")
    print(f"quoted the frontmatter description in {skill_md}")
PY

echo "Re-vendored ${#SKILLS[@]} dial9 skills from $REPO @ $(git -C "$TMP/dial9" rev-parse HEAD)"
echo "Remember to update the pinned commit in README.md and this script."
