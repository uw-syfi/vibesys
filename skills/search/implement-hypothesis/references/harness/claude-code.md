# Claude Code

## Locate a skill's own scripts

`${CLAUDE_SKILL_DIR}` resolves to the directory of the skill that contains the text. A sibling
skill's script: `python3 "${CLAUDE_SKILL_DIR}/../measure/scripts/measurements.py"`. Its own:
`python3 "${CLAUDE_SKILL_DIR}/scripts/measurements.py"`.
