# Codex

## Run a long command as a tracked background task

Start the command with `exec_command`. When it is still running after `yield_time_ms`, the
result carries a `session_id` and the command keeps running as a background terminal. Codex
sends no completion notification, so poll it: `write_stdin` with that `session_id` and empty
`chars` waits for output (5 to 300 seconds) and reports the exit code once the command ends.
`/ps` lists background terminals and `/stop` stops them all. Run the command
in the foreground of that session, not behind `nohup`, `setsid` or a shell `&`, so the poll sees
its output and exit code. If a PID is needed (to kill by PID later), have the command write `$$`
to a file before it runs `exec`, and read that file.

## Locate a skill's own scripts

The skill list Codex shows names each skill with `(file: <path>/SKILL.md)`. The skill's
directory is that file's parent. Its own script: `python3 "<directory>/scripts/measurements.py"`.
A sibling skill's: `python3 "<directory>/../measure/scripts/measurements.py"`. Plugin installs
and `npx skills` installs both keep the skills side by side, so the sibling path holds.

## Wait for work without polling

Codex has no notification for a finished background terminal, so waiting means a bounded
blocking call: an empty `write_stdin` poll. Never use a foreground `sleep`. Do not end the turn
while a command you started is still running; Codex does not resume a finished turn by itself.
