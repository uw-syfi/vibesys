# Claude Code

## Locate a skill's own scripts

`${CLAUDE_SKILL_DIR}` resolves to the directory of the skill that contains the text. Analyze a
copied-back capture with `python3 "${CLAUDE_SKILL_DIR}/scripts/analyze.py" <group> ...`. Copy the
capture wrapper into the candidate repository with
`cp "${CLAUDE_SKILL_DIR}/scripts/capture.py" tools/capture.py`, then commit it.
