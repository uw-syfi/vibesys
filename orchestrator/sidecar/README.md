# Orchestrator sidecar

One Python process per agent session. It drives a provider (Claude Code or Codex) through
[agentshim](https://github.com/vic-lsh/agentshim) and serves `SidecarService`
(`proto/vibesys/orchestrator/v1/sidecar.proto`) to the orchestrator daemon over a Unix socket.
It owns the agent process and outlives the daemon: a daemon that restarts calls `Follow` with the
last offset it committed and loses only what it had not read.

This is a separate project with its own `pyproject.toml` and `uv.lock`; the repository's Python
packages do not import it and it imports none of them. Provider knowledge lives in agentshim.
The only provider names here are the `Harness` to provider table in `driver.py`.

## Layout

| Module | Role |
|---|---|
| `normalize.py` | Pure fold from agentshim events to sidecar events. Same events, same output. |
| `store.py` | `Store` protocol, `MemoryStore`, `FileStore` (`events.log`, `raw.log`, `effects.log`). Offsets, effect ids, torn-tail repair. |
| `driver.py` | `Driver` protocol and `AgentshimDriver`; builds the agentshim session (subagents off, text streaming, MCP identity env). |
| `engine.py` | Carries out commands, drops repeated effect ids, numbers events, reports a lost session on restart. No threads, no sockets. |
| `prompts.py`, `prompts/` | The words sent to the agent for a `Message`, rendered from templates. |
| `server.py`, `main.py` | The thin shell: gRPC over a Unix socket, a worker thread, process start. |
| `gen/` | Generated bindings. `./generate.sh` rewrites them. |

## Running one

```
python -m vs_sidecar --launch launch.json --state-dir DIR --socket DIR/sidecar.sock \
    [--worktree PATH] [--mcp-config mcp.json] [--detach]
```

`launch.json` is a `LaunchSession` in protobuf JSON. `mcp.json` is a list of
`{"name", "command", "args", "env"}` stdio servers; each gets `VIBESYS_AGENT_ID`,
`VIBESYS_RUN_ID` and `VIBESYS_SESSION` in its environment. `--detach` double-forks and logs to
`DIR/sidecar.log`; a daemon can instead start it in its own session.

## Behaviour a client can rely on

- **Effects.** An effect id is recorded before its command runs, in `effects.log`. A repeat, also
  after a restart, answers `Send` successfully and does nothing.
- **Offsets.** Every event, deltas included, takes the next offset from 1, and is synced to
  `events.log` before `Follow` can return it.
- **Restart.** A restarted sidecar has lost the agent process. It ends each open turn `FAILED`,
  emits `SessionFailed`, and takes no more turns; `Close` still works. The daemon launches a new
  session with `resume_conversation_id` from `ConversationOpened`.
- **Items.** Ids are the provider's (Claude `<message id>:<block index>` and the tool call id;
  Codex item ids). An item still running when its turn ends is finished if the turn completed and
  failed otherwise.
- **Usage.** Reports replace each other, so cumulative totals are not counted twice. Counts a
  provider does not report are absent.
- **Steers.** A steer the provider consumes answers `accepted`. One the turn came too late for, or
  that the provider refused, answers `accepted = false` after that turn's `TurnEnded`.
- **Between turns.** Activity the provider reports while no turn runs is filed under the last
  turn's id (or `idle` before the first).
- **Raw log.** `raw.log` has every line crossing the provider pipe with the offset it followed,
  for replay fixtures.

## Checks

```
uv run ruff check . && uv run pytest
./generate.sh        # after a proto change
```

`conformance/` holds the scenarios shared with a Go Fake of this service.
