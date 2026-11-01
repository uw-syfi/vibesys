# Claude Code

## Run a long command as a tracked background task

Use the Bash tool with `run_in_background: true`. A completion notification arrives when the
command exits. Never use `nohup`, `setsid` or a shell `&`: Claude Code then cannot track the
command and no notification arrives. If a PID is needed (to kill by PID later), have the
command write `$$` to a file before it runs `exec`, and read that file.

## Locate a skill's own scripts

`${CLAUDE_SKILL_DIR}` resolves to the directory of the skill that contains the text. A sibling
skill's script: `python3 "${CLAUDE_SKILL_DIR}/../measure/scripts/measurements.py"`. Its own:
`python3 "${CLAUDE_SKILL_DIR}/scripts/measurements.py"`.

## Wait for work without polling

Rely on the background-task and subagent notifications; end the turn to yield. Never use a
foreground `sleep`.
