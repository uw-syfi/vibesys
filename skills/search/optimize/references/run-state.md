# Run controller

`python3 "${CLAUDE_SKILL_DIR}/scripts/run.py"` owns run lifecycle state.
The installed plugin needs only Python 3.10+ and git on POSIX (Linux or macOS). Convert the requested duration to
seconds, then `start --task <task> --seconds <seconds> --fanout <N>` prints the absolute run
directory. Save it as `$RUN`. Never edit `run.json`, `ledger.md` or the active marker.

Every other command takes `--run "$RUN"`:

| Command | Arguments and effect |
| --- | --- |
| `status` | Prints time left, wrap-up, open items, free slots and next action |
| `add` | `--id <id> --kind implement\|profile\|combine\|judge --hypothesis <text>`; records dispatch and consumes a slot |
| `transition` | `--id <id> --to returned\|judging\|won\|parked\|abandoned --reason <text>` |
| `judge` | `--id <id> --commit <sha> --result pass\|fail\|unmeasured --reason <evidence>`; records the independent judge's own check |
| `move-main` | `--id <id>`; only a won item with judge pass, check pass and `better` against current main |
| `track` | `--id <Bash-task-id> --pid <saved-PID> --description <text>`; registers an owned background command |
| `untrack` | `--id <Bash-task-id>`; refuses while that PID is alive; remove only after completion or confirmed cancellation |
| `idle` | `--reason <text>`; records why slots stay idle until an item returns or main moves |
| `finish` | Before wrap-up requires `--early --reason <text>`; always requires all items terminal, no tracked commands, fresh successful measurement of main |

Legal transitions are dispatched to returned or abandoned, returned to judging, parked or
abandoned, and judging to returned, won, parked or abandoned. Won requires a recorded judge pass.
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
Before wrap-up, free slots without a current idle reason block Stop, even while
subagents or registered shells run. The message is exactly `status.next`.
A current idle reason or zero free slots releases search turns. A cron alone and `stop_hook_active` do not release. During wrap-up it blocks until finish or
deadline. Unreadable state fails open. The deadline bounds total blocking; Claude Code's
additional eight consecutive tool-free continuation cap bounds an inert lead (a tool call
resets that cap). Neither bound substitutes for finishing successfully.

`finish` uses the same full record pool as `measurements.py compare --commit <main>
--base <base> --json`, while separately requiring a fresh post-move successful record.
The report names contributing record ids as 1-based `measurements.jsonl` record ordinals.

## Recovery and state schema

`abort --run "$RUN" --reason <why>` closes a crashed or expired run and removes its active
marker. It does not stop processes: inspect and stop owned resources first. Recover a foreign
or reused PID with `untrack --force --reason <evidence>`; the decision is recorded.
An `unmeasured` judge records no pass. Transition judging -> returned, measure, then judge again.

| run.json field | Meaning |
| --- | --- |
| schema | Format version (1) |
| run_id, task | Run identity and task directory name |
| started_at, deadline, wrap_up_at | Zoned ISO instants bounding scheduling |
| fanout | Shared worker/judge slot limit |
| base_commit | Immutable initial revision |
| main_branch, main_commit, main_moved_at | Owned winner and last movement instant |
| items | IDs to kind, hypothesis, lifecycle state, reason and judge evidence |
| commands | Bash task IDs to description and PID |
| session_id | Lead session, bound by the first Stop hook; other sessions release |
| pending_move | Durable promotion intent, or null |
| promotions | Reconciled winner moves with comparisons and timestamps |
| idle_reason | Current reason, invalidated by a return or main movement |
| decisions | Timestamped idle, early-finish, force-untrack and abort decisions |
| finished | Closed lifecycle flag |

| Window | Free slots / current reason | Stop |
| --- | --- | --- |
| Search | Free slots, no reason, any running tasks | Block with exact status.next |
| Search | No free slots, or current reason | Release |
| Wrap-up | Any | Block with exact status.next |
| Deadline / finished / other session | Any | Release |

Legacy schema-1 runs without `session_id` remain readable: the next transaction writes
the new field, and the first Stop binds the lead session. This allows `abort` to recover
a run created before session ownership was added. Other missing or unknown fields fail.
