# Codex

## Dispatch a subagent with a fresh context

Call `spawn_agent` with the brief as `message`. Pass `fork_turns: "none"` (or
`fork_context: false`, in builds whose schema has that field instead): newer builds default to
forking the whole lead history into the subagent. Leave `model` and `reasoning_effort` unset
unless the optimization's instructions ask for one. A subagent's final answer is delivered to
the lead when it finishes; `wait_agent` waits for it. `send_input` or `followup_task`
continues a subagent, `list_agents` shows which exist, and `interrupt_agent` stops its turn.
A finished subagent still counts toward the concurrency limit until `close_agent` closes it, so
close each one after reading its result. The limit is `agents.max_concurrent_threads_per_session`
in `config.toml` (legacy name `agents.max_threads`); keep it above `--fanout`.

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

## Schedule periodic work

Codex CLI has no scheduler a running session can use; scheduled tasks live in the Codex app.
Instead, report whenever a subagent or background command finishes, and at each wait.

## Wait for work without polling

Block on `wait_agent` for subagents and on an empty `write_stdin` poll for background
commands; both return when something happens or their timeout passes. Never use a foreground
`sleep`. Do not end the turn while subagents or commands are running: Codex does not resume a
finished turn by itself.

## Packaging

Codex reads the repository's `.claude-plugin/marketplace.json`, so one manifest serves both
harnesses:

```bash
codex plugin marketplace add uw-syfi/vibesys
codex plugin add vibesys@vibesys
```

Skills then appear with the `vibesys:` prefix, as `vibesys:optimize`. Start the loop with
`$vibesys:optimize <task> [--fanout N]` or choose it in `/skills`. Skills installed with
`npx skills add` have no prefix: read `vibesys:measure` as the skill `measure`.
