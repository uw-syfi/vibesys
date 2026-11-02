# Claude Code

## Locate a skill's own scripts

`${CLAUDE_SKILL_DIR}` resolves to the directory of the skill that contains the text. This
skill's script: `python3 "${CLAUDE_SKILL_DIR}/scripts/commit_schema.py"`. From a sibling skill:
`python3 "${CLAUDE_SKILL_DIR}/../commit/scripts/commit_schema.py"`.
