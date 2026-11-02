# Claude Code

## Run a long command as a tracked background task

Use the Bash tool with `run_in_background: true` for `vibesys-cli wait ...`. A completion
notification arrives when the command exits. Never use `nohup`, `setsid` or a shell `&`: Claude
Code then cannot track the command and no notification arrives.

## Wait for work without polling

Rely on the background-task and subagent notifications; end the turn to yield. Never use a
foreground `sleep`.
