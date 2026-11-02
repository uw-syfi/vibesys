# SidecarService conformance

Scenarios any implementation of `SidecarService` must pass: the Python sidecar, and the Go Fake
the daemon tests against. A scenario is one JSON file in `scenarios/`:

- `provider`: what the provider does in each turn, in order. A turn is a list of abstract steps:
  `{"think": text, "item": id}`, `{"say": text, "item": id}`,
  `{"tool": name, "id": call, "output": text, "exit_code": n}` (a call and its result),
  `{"tool_left_running": command, "id": call}` (a call that never reports a result) and
  `{"usage": {"input": n, "output": n}}` (a cumulative report). An implementation maps these to
  whatever drives it (the Python runner builds agentshim events; a Fake plays them directly).
- `steps`: `{"send": SidecarCommand}` (protobuf JSON, so `startTurn.turnId`) sends a command;
  `{"await": {...}}` follows the log from the start until `turn_ended` (a turn id), `steer` (a
  message id) or `closed` shows up. Nothing else waits, so no scenario depends on time.
- `checks`: the properties the whole log must have afterwards, by name (below).
- `expect`: data a check reads.

The Python runner is `tests/conformance.py`; `tests/test_conformance.py` runs every scenario over a
real Unix socket. The Go side reads the same files and implements the same check names.

## Checks

| Check | Holds when |
|---|---|
| `opened_first` | the first event is `ConversationOpened`, and there is one |
| `offsets_increase` | offsets start at 1 or more and strictly increase (they may skip: deltas take offsets) |
| `one_end_per_turn` | every turn the scenario starts has exactly one `TurnEnded` |
| `items_precede_end` | no item of a turn is reported after that turn's `TurnEnded` |
| `no_running_item_after_end` | the last report of every item is not `RUNNING` |
| `usage_ends_the_turn` | `TurnEnded.usage` equals the turn's last `UsageReported` (reports replace, never add) |
| `effect_repeat_dropped` | the scenario repeats an effect id and every turn still ends exactly once |
| `follow_resumes` | `Follow(after_offset=k)` returns exactly the events with offset above `k` |
| `late_steer_refused` | a `SteerOutcome` for each message in `expect.steer` has the stated `accepted` and follows a `TurnEnded` |
| `close_is_last` | `SessionClosed` is the last event and appears once |

Adding a scenario or a check changes this contract: update this table and the Go Fake with it.
