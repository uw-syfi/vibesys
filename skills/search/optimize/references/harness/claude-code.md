# Claude Code

## Dispatch a subagent with a fresh context

Use the Agent tool with a general-purpose subagent and `run_in_background: true`. When it
finishes, a notification with its result arrives in the lead's turn. SendMessage continues a
running or finished agent, ListAgents shows which exist, and TaskStop stops one.

## Run a long command as a tracked background task

Use the Bash tool with `run_in_background: true`. A completion notification arrives when the
command exits. Never use `nohup`, `setsid` or a shell `&`: Claude Code then cannot track the
command and no notification arrives.

## Schedule periodic work

CronCreate, CronList and CronDelete create, list and delete schedules. They last only for the
session, so re-arm them after a resume. A report is a message to the user. Give the final report
as your last message, start it with the words "Final report", and end the turn.

## Wait for work without polling

Rely on the background-task and subagent notifications; end the turn to yield, but only while a
subagent or a background command is running, since only they send a notification. Never use a
foreground `sleep`.

## Packaging

The plugin layout is `.claude-plugin/marketplace.json` at the repository root, with the plugin
source at `skills/`. Invoke the loop as `/vibesys:optimize <task> [--fanout N]`.
