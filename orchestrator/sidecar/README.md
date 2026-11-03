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
| `driver.py` | `Driver` protocol and `AgentshimDriver`; builds the agentshim session (subagents off, text streaming, MCP identity env, the launch's plugin). |
| `engine.py` | Carries out commands, drops repeated effect ids, numbers events, reports a lost session on restart. No threads, no sockets. |
| `prompts.py`, `prompts/` | The words sent to the agent for a `Message`, rendered from templates. |
| `server.py`, `main.py` | The thin shell: gRPC over a Unix socket, a worker thread, process start. |
| `gen/` | Generated bindings. `./generate.sh` rewrites them. |
| `e2e/` | The scripted sidecar the end to end tests launch, and the acting peers behind it (see below). Test support; the product never imports it. |

## The plugin

`launch.plugin` names the marketplace checkout, the plugin and the plugin home. `build_session`
hands them to agentshim as `SessionPlugins`, so both harnesses load that plugin and the project's own
configuration and nothing of the operator's plugins, skills, settings or memory. The launch has no
default: a missing plugin, a relative path, a marketplace without a manifest, or a name the manifest
does not hold is refused before an agent starts, naming the field. The home belongs to the agent and
is reused by each of its sessions, because the provider keeps the conversation there too.

## Running one

```
python -m vs_sidecar --launch launch.json --state-dir DIR --socket DIR/sidecar.sock \
    [--worktree PATH] [--mcp-config mcp.json] [--detach]
```

`launch.json` is a `LaunchSession` in protobuf JSON, validated at start. The agent needs a
directory: the launch must carry `worktree` (the daemon creates it and passes it in every
launch), or `--worktree` fills it in; with neither the process exits with code 2 naming the key. `mcp.json` is a list of
`{"name", "command", "args", "env"}` stdio servers; each gets the session's identity, laid over its
own `env`, so a server cannot be told it is another agent. `--detach` double-forks and logs to
`DIR/sidecar.log`; a daemon can instead start it in its own session.

### The agent's environment

The sidecar builds one environment per session and gives it to the agent's processes and to every
tool server: the login shell's environment (agentshim builds a provider's environment from that
probe and never from the sidecar process's own) with the session's identity laid over it. The names,
what fills each and who reads it are in `proto/vibesys/cli/v1/conformance/identity_env.golden.json`,
which the sidecar's tests and the vibesys-cli's tests both read:

| Name | Filled from the launch |
| --- | --- |
| `VIBESYS_REPO`, `VIBESYS_TASK`, `VIBESYS_RUN`, `VIBESYS_AGENT` | `repository`, `task`, `run_id` and `session.agent_id`: the vibesys-cli's flag defaults |
| `VIBESYS_HARNESS` | the harness, as the CLI names it (`claude-code`, `codex`) |
| `VIBESYS_SESSION` | `session.session` |
| `VIBESYS_CLI` | `cli_location.cli`: the vibesys-cli the orchestrator chose; the skills run `"$VIBESYS_CLI"` |
| `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `HOME` | `cli_location`: where that CLI finds its daemon and store |

A location value that is empty unsets its name, so the agent resolves the place as the orchestrator's
own process would; `HOME` is the exception and is kept, because the provider's own CLI needs it.

## Behaviour a client can rely on

- **Effects.** An effect id is recorded before its command runs, in `effects.log`. A repeat, also
  after a restart, answers `Send` successfully and does nothing.
- **Offsets.** Every event, deltas included, takes the next offset from 1, and is synced to
  `events.log` before `Follow` can return it. A write that fails is cut off the file and raised,
  so the log never holds an event `Follow` did not show and an offset is never given to two
  events. A store failure is fatal: the process closes the agent and exits, and a restart reports
  the session lost.
- **Restart.** A restarted sidecar has lost the agent process. It settles every item still
  `RUNNING` as `FAILED` (an open turn's items before that turn's end), ends each open turn
  `FAILED`, emits `SessionFailed`, and takes no more turns; `Close` still works. The daemon
  launches a new session with `resume_conversation_id` from `ConversationOpened`. A turn that
  began and left nothing in the log before the crash has no `TurnEnded`: treat `SessionFailed` as
  the end of every turn you started and did not see end.
- **Items.** Ids are the provider's (Claude `<message id>:<block index>` and the tool call id;
  Codex item ids). An item still running when its turn ends is finished if the turn completed and
  failed otherwise.
- **Usage.** A turn's usage is its result's, reported once (`UsageReported`, then `TurnEnded.usage`
  carries the same). Reports the provider makes during a turn are not turn totals (Claude reports
  each message on its own; Codex reports the turn's increment) and are not passed on, so a turn
  that fails without a result ends without usage. Counts a provider does not report are absent.
  Activity between turns reports no usage.
- **Interrupt and steer** name a turn, and act on that turn only. An interrupt for a queued turn
  ends it `INTERRUPTED` without running it; one that arrives after the turn began and before the
  provider started it still ends it; one for a turn that already ended does nothing. A steer the
  provider consumes answers `accepted`; one it rejects answers `accepted = false` at once; one the
  turn came too late for answers `accepted = false` after that turn's `TurnEnded`. A steer never
  reaches the turn after the one it names.
- **Between turns.** Activity the provider reports while no turn runs is filed under the last
  turn's id (or `idle` before the first), one fold per stretch, with ids that cannot collide with
  the turn's. Items still running when the stretch ends (a turn begins, the session fails or
  closes) are settled `FAILED`.
- **Raw log.** `raw.log` has every line crossing the provider pipe with the offset it followed,
  written without a sync and never under the engine's lock. It is for inspection and fixtures; no
  test replays it into the event log.

## Process lifetime

The agent is a child of the sidecar and leads a process group of its own (agentshim starts it in a
new session). If the sidecar is stopped with SIGTERM or SIGINT, or fails (a store write error, a
bug), it interrupts the running turn, closes the agent, and exits. If it is killed with SIGKILL it
cannot, and the agent runs until its stdin closes, so **the daemon reaps it**. The state
directory has what it needs:

- `sidecar.pid`: the sidecar's process id.
- `agent.pgids`: one process-group id per line, for each group led by a direct child of the
  sidecar (the agent). It is checked every 0.25 s, including during a turn, and rewritten when
  the set changes; a sidecar that exits on its own empties it after closing the agent.

To clean up a sidecar that is gone, the daemon sends SIGKILL to each group in `agent.pgids`
(`kill(-pgid)`) after checking that `sidecar.pid` is not running, and before it launches a
resumed session on the same worktree, so no old agent edits the worktree under the new one.
Nothing in the sidecar does this; the daemon owns reaping.

## Checks

```
uv run ruff check . && uv run pytest
./generate.sh        # after a proto change
```

`conformance/` holds the scenarios shared with a Go Fake of this service.

## The scripted sidecar for end to end tests

`e2e/scripted_sidecar.py` is the real sidecar over agentshim's scripted Claude and Codex peers, so
no model is called. It has two modes, chosen by `VIBESYS_E2E_SCRIPT`:

- `reply` (default): each message gets a canned reply.
- `act`: each role runs a script (`e2e/scripts.py`) whose steps are real effects, shell commands
  and the orchestrator's MCP tools, performed through `e2e/host.py`, and the peer's turn mirrors
  them with their real output. The lead spawns two workers and a judge, workers commit, check and
  measure with `vibesys-cli`, the judge compares, and the lead promotes and reports. A message
  beyond a script's steps fails the turn and writes `peer-failure.txt` in the session state
  directory. `prompts/N.md` and `spawns.json` (argv, provider home, plugin commands; the MCP token
  is redacted) record what the agent was sent and how it was launched.

`gateway/e2e/desktop_path_test.go` drives this through the desktop path (`desktop_path_e2e` check
group, CI job `desktop-path`). With `VIBESYS_E2E_DESKTOP_UI=1` it also launches the built desktop
app against the same world and checks the run list, each agent's harness and the lead's report on
screen, then presses Stop (`clients/apps/desktop/e2e`). The scripts are pure generators over a `Machine` protocol, so
`tests/test_e2e_scripts.py` runs them against a Fake without processes.
