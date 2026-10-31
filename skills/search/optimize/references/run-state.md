# Run controller

`python3 "${CLAUDE_SKILL_DIR}/scripts/run.py"` owns run lifecycle state.
The installed plugin needs only Python 3.10+ and git. Convert the requested duration to
seconds, then `start --task <task> --seconds <seconds> --fanout <N>` prints the absolute run
directory. Save it as `$RUN`. Never edit `run.json`, `ledger.md` or the active marker.

Every other command takes `--run "$RUN"`:

| Command | Arguments and effect |
| --- | --- |
| `status` | Prints time left, wrap-up, open items, free slots and next action |
| `add` | `--id <id> --kind implement\|profile\|combine\|judge --hypothesis <text>`; records dispatch and consumes a slot |
| `transition` | `--id <id> --to returned\|judging\|won\|parked\|abandoned --reason <text>` |
| `judge` | `--id <id> --commit <sha> --result pass\|fail --reason <evidence>`; records the independent judge's own check |
| `move-main` | `--id <id>`; only a won item with judge pass, check pass and `better` against current main |
| `track` | `--id <Bash-task-id> --pid <saved-PID> --description <text>`; registers an owned background command |
| `untrack` | `--id <Bash-task-id>`; refuses while that PID is alive; remove only after completion or confirmed cancellation |
| `idle` | `--reason <text>`; records why slots stay idle until an item returns or main moves |
| `finish` | Before wrap-up requires `--early --reason <text>`; always requires all items terminal, no tracked commands, fresh successful measurement of main |

Legal transitions are dispatched to returned or abandoned, returned to judging, parked or
abandoned, and judging to won, parked or abandoned. Won requires a recorded judge pass.
Returned items release their worker slot; judging consumes one. Judges share the fanout cap.
Promotion requires `better`; `within_noise` is parked even with a judge pass.

The controller owns `run.json` (metadata, items, commands, promotion intent), rendered
`ledger.md`, the git exclude entry, main's branch/worktree, the active marker, and the numeric
section of `report.md`. `measurements.py` alone owns `measurements.jsonl`; the controller uses
its public reader and comparison. The lead owns prose notes and appends report prose.
State is atomically replaced; ledger is regenerated on every command. A persisted promotion
intent is reconciled against git on the next command; a recovered move requires a new final
measurement. Only the lead runs mutating commands, serially; workers return evidence.
Do not move main by hand. Use `vibesys-work/<run-id>/<work-id>` for candidate branches.

Wrap-up starts `min(1200 seconds, 25% of duration)` before deadline. The time box is a
limit, not a quota: `finish --early --reason <text>` records an early completion decision. Stop owned tasks before finishing, then cancel the one owned wakeup. At deadline the
hook releases even if verification is incomplete; report that explicitly.

# Stop hook contract

The [official Stop input reference](https://code.claude.com/docs/en/hooks#stop-input)
documents `background_tasks` entries with `id`, `type`, `status`, and separate `session_crons`.
The hook releases search turns only for running subagents or registered running shell tasks.
A current recorded idle reason or zero free slots also releases search turns.
With free slots, no current reason and nothing running, the hook blocks and names both
`add` and `idle --reason`. A cron alone and `stop_hook_active` do not release. During wrap-up it blocks until finish or
deadline. Unreadable state fails open. The deadline bounds total blocking; Claude Code's
additional eight consecutive tool-free continuation cap bounds an inert lead (a tool call
resets that cap). Neither bound substitutes for finishing successfully.

Trial 2's `stop_hook_summary` records, in session
`cc57aa3d-e0a2-4ac1-a31a-8ff77f102841`, show all 11 hooks with
`preventedContinuation: false` and `hasOutput: false`, including nested-directory stops at
01:30:14 and 01:36:39 UTC. Those summaries prove the outcome, not the raw hook input arrays.
The documented array schemas, rather than inferred summary fields, determine this hook.

`finish` uses the same full record pool as `measurements.py compare --commit <main>
--base <base> --json`, while separately requiring a fresh post-move successful record.
The report names contributing record ids as 1-based `measurements.jsonl` record ordinals.
