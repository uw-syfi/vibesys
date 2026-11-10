# Claude Code

## Locate a skill's own scripts

`${CLAUDE_SKILL_DIR}` resolves to the directory of the skill that contains the text. This
skill's script: `sh "${CLAUDE_SKILL_DIR}/scripts/cli_version.sh"`. From a sibling skill:
`sh "${CLAUDE_SKILL_DIR}/../setup/scripts/cli_version.sh"`.
