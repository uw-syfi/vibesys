# Codex

## Run a long command as a tracked background task

Start `vibesys-cli wait ...` with `exec_command`. When it is still running after `yield_time_ms`,
the result carries a `session_id` and the command keeps running as a background terminal. Codex
sends no completion notification, so poll it: `write_stdin` with that `session_id` and empty
`chars` waits for output (5 to 300 seconds) and reports the exit code once the command ends.
`/ps` lists background terminals and `/stop` stops them all. Run the command in the foreground of
that session, not behind `nohup`, `setsid` or a shell `&`, so the poll sees its output and exit
code. A `wait` that times out leaves its jobs running.

## Wait for work without polling

Codex has no notification for a finished background terminal, so waiting means a bounded
blocking call: an empty `write_stdin` poll. Never use a foreground `sleep`. Do not end the turn
while a command you started is still running; Codex does not resume a finished turn by itself.
