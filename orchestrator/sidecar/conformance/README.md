# AgentSessionService conformance

Scenarios any implementation of `AgentSessionService` must pass: the Python one, and the Go Fake
the host's tests run against. A scenario is one JSON file in `scenarios/`:

- `provider`: what the provider does in each turn, in order. A turn is a list of abstract steps:
  `{"think": text, "item": id}`, `{"say": text, "item": id}`,
  `{"tool": name, "id": call, "output": text, "exit_code": n}` (a call and its result),
  `{"tool_left_running": command, "id": call}` (a call that never reports a result),
  `{"report": {"input": n, "output": n}}` (a usage report during the turn, which is not a total),
  `{"total": {"input": n, "output": n}}` (the usage of the turn's result), `{"end": "failed"}`
  (the turn fails, with no result), `{"crash": true}` (the process dies here) and `{"start_fails": REASON, "detail": text}` (a turn of its own: the session cannot start, for the `StartFailureReason` named without its prefix). An
  implementation maps these to whatever drives it (the Python runner builds agentshim events; a
  Fake plays them directly).
- `steps`: a scenario with no `hello`, `open` or `refused` step is opened by the runner first, with
  `open_request.json`, and again after each restart. Otherwise its steps do the opening.
  `{"hello": {"compatibility": NAME, "session": ref or null}}` says hello as a peer of this build, or
  with `client` for other versions, and checks the answer;
  `{"open": {fields}}` opens with `open_request.json` with those top-level fields replaced;
  `{"refused": {"open": {fields} | "send": SessionCommand | "follow": true, "code": NAME, "names": text}}`
  makes a call that must be refused with that status code and a message holding `names`;
  `{"send": SessionCommand}` (protobuf JSON, so `startTurn.turnId`) sends a command;
  `{"await": {...}}` follows the log from the start until `turn_ended` (a turn id), `steer` (a
  message id), `closed`, `failed` (a `SessionFailed`) or `item` (an item id, once finished) shows
  up; `{"restart": true}` kills the process where it stands (after a `crash` step, once the
  scenario has awaited an event that precedes it) and starts another on the state the first left.
  Nothing else waits, so no scenario depends on time.
- `checks`: the properties the whole log must have afterwards, by name (below).
- `expect`: data a check reads.

The Python runner is `tests/conformance.py`; `tests/test_conformance.py` runs every scenario over a
real Unix socket. The host's tests play the scenarios about the time before a session against the holder they run on (`internal/production/holders_sim_test.go`). The Go daemon tests read the scenarios about a session that cannot start (`internal/daemon/conformance_test.go`): the Fake plays what the file says a sidecar sends and the daemon must record the reason it names.

## Checks

| Check | Holds when |
|---|---|
| `opened_first` | the first event is `SessionOpened`, and there is one |
| `one_agent_started` | the process started exactly one agent, however often it was opened |
| `no_agent_started` | the process started no agent |
| `offsets_increase` | offsets start at 1 or more and strictly increase (they may skip: deltas take offsets) |
| `one_end_per_turn` | every turn the scenario starts has exactly one `TurnEnded` |
| `items_precede_end` | no item of a turn is reported after that turn's `TurnEnded` |
| `no_running_item_after_end` | the last report of every item is not `RUNNING` |
| `usage_is_the_turn_result` | for each turn in `expect.usage`: the turn's one `UsageReported` equals its `TurnEnded.usage` and the counts match, or, for `null`, the turn has neither (provider reports during a turn are not totals and are not passed on) |
| `session_lost_after_restart` | exactly one `SessionFailed`, and each turn in `expect.failed_turns` has its one `TurnEnded` (`FAILED`) before it |
| `effect_repeat_dropped` | the scenario repeats an effect id and every turn still ends exactly once |
| `follow_resumes` | `Follow(after_offset=k)` returns exactly the events with offset above `k` |
| `late_steer_refused` | a `SteerOutcome` for each message in `expect.steer` has the stated `accepted` and follows a `TurnEnded` |
| `close_is_last` | `SessionClosed` is the last event and appears once |
| `start_failure_is_typed` | the log is exactly one `SessionFailed` (no `SessionOpened`), its `start_failure_reason` is `expect.start_failure`, and its detail begins "could not start the agent: " |

Adding a scenario or a check changes this contract: update this table and the Go Fake with it.
