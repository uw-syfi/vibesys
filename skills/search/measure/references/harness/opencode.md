# opencode

## Run a long command as a tracked background task

The `bash` tool has no background mode, and a command behind `nohup`, `setsid` or a shell `&` is
not tracked. A lead (an agent that can start subagents) starts a background subagent with the
`task` tool (`background: true`) whose brief is only: run `vibesys-cli wait --timeout
<duration> <job>...` (a duration such as `30m`) in the foreground with a `bash` `timeout`, in
milliseconds, longer than that duration, then reply with its output and exit code. Its reply
arrives as a completion message that starts the lead's next turn. A subagent, which cannot start
one, runs that `wait` in the foreground itself with such a `bash` `timeout`. A `wait` that times
out leaves its jobs running.

## Wait for work without polling

Rely on the background subagents' completion messages; end the turn to yield, but only while one
is running. A subagent blocks on its foreground `wait` instead. Never use a foreground `sleep`.
