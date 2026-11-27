# opencode

## Dispatch a subagent with a fresh context

Call the `task` tool with `subagent_type: "general"`, the brief as `prompt`, a short
`description`, and `background: true`. The subagent starts with no history of yours. The call
returns at once with the subagent's task id; when the subagent finishes, its last message arrives
in your session as a `<task id="..." state="completed">` message and starts a new turn of yours.
`task` with that `task_id` and a new `prompt` continues a finished subagent. opencode has no tool
that lists or stops subagents: keep the task id, plan and role of each one you start, and count
the running ones against `--fanout` yourself. A subagent you no longer need runs until it hands
off, so send a worker its brief only when you mean it to finish.

Background subagents need `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the environment
opencode starts with (`task` refuses `background` without it, naming the variable), and a
long-lived opencode process (see Packaging). Subagents cannot start subagents of their own unless
`subagent_depth` is raised in the config.

## Run a long command as a tracked background task

The `bash` tool has no background mode, and a command behind `nohup`, `setsid` or a shell `&` is
not tracked. `vibesys-cli submit` returns once the job is queued, so submitting needs nothing
more. To be woken when jobs end, start a background subagent (as above) whose brief is only: run
`vibesys-cli wait --timeout <duration> <job>...` (a duration such as `30m`) in the foreground
with a `bash` `timeout`, in milliseconds, longer than that duration, then reply with its output
and exit code. Its reply is the completion notification. Such a waiter writes nothing, so it does
not count against `--fanout`. A `wait` that times out leaves its jobs running.

## Schedule periodic work

opencode has no scheduler a session can use. Instead, give a progress report after a promotion,
and at a wait when an hour has passed since the last. There is no report tool: a report is a
message to the user. Give the final report as your last message, start it with the words "Final
report", and end the turn.

## Wait for work without polling

Rely on the background subagents' completion messages: end the turn to yield, but only while a
background subagent is running, since only they start a new turn. Do not start a subagent, in the
foreground or the background, only to pass time. Never use a foreground `sleep` or a loop over
`vibesys-cli jobs`. A subagent waits on its own jobs in the foreground, with a
`bash` `timeout` that covers the wait.

## Packaging

opencode reads Agent Skills from `.opencode/skills/`, `.claude/skills/` and `.agents/skills/`
in the project, from `~/.config/opencode/skills/`, `~/.claude/skills/` and `~/.agents/skills/`,
and from any directory its config lists under `skills.paths`. Copy the `search/` skill folders,
and the domain skills the task needs, side by side into one of them, or install them with
`npx skills`. `opencode debug skill` lists what it found. Skills carry no plugin prefix: read
`vibesys:measure` as the skill `measure`. Start the loop by asking for the
`optimize` skill with its arguments, for example `Load the optimize skill and run optimize
<task> --fanout 3`.

Run the lead in a process that outlives its turns: the interactive `opencode`, or `opencode
serve` with the session driven by `opencode run --attach <url>`. A plain `opencode run` exits
when the lead's turn ends and stops every background subagent with it.
