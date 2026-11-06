# Orchestrator sidecar

One Python process per agent session. It drives a provider (Claude Code or Codex) through
[agentshim](https://github.com/vic-lsh/agentshim) and serves `AgentSessionService`
(`proto/vibesys/orchestrator/v1/agent_session.proto`) to the orchestrator daemon over a Unix socket.
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
| `driver.py` | `Driver` protocol and `AgentshimDriver`; builds the agentshim session from an `OpenSessionRequest` (subagents off, text streaming, its tool servers, environment and skill bundle). |
| `holder.py` | What a process that holds a session answers before it holds one: `Hello` and `Open`. |
| `engine.py` | Carries out commands, drops repeated effect ids, numbers events, reports a lost session on restart. No threads, no sockets. |
| `server.py`, `main.py` | The thin shell: gRPC over a Unix socket, a worker thread, process start. |
| `gen/` | Generated bindings. `./generate.sh` rewrites them. |
| `e2e/` | The scripted sidecar the end to end tests launch, and the acting peers behind it (see below). Test support; the product never imports it. |

## The skill bundle

`OpenSessionRequest.skills` names the bundle's directory and the plugin in it, and
`agent_data_directory` is where the provider keeps its configuration. `build_session` hands them to
agentshim as `SessionPlugins`, so both harnesses load that plugin and the project's own configuration
and nothing of the operator's plugins, skills, settings or memory. Nothing here reads the bundle:
agentshim does, and a bundle it cannot load comes back as the start's failure, `PLUGIN_UNAVAILABLE`.
The data directory belongs to the agent and is reused by each of its sessions, because the provider
keeps the conversation there too.

## Running one

```
python -m vs_sidecar < bootstrap
```

The process takes no arguments. Its standard input is one `SessionBootstrap` in protobuf binary
form: the socket to listen on and its private state directory. Whoever starts it gives it a session
of its own, and its log as stdout and stderr; it does not fork. A bad bootstrap exits with code 2
naming the field.

It then serves `AgentSessionService` and holds nothing until it is asked:

| Call | What it does |
| --- | --- |
| `Hello` | Says which versions it is, whether it can work with the caller, and which session it holds, if any. It does no work, and answers while an agent is starting. |
| `Open` | Takes everything a session needs in one message, refusing a bad field by name with `INVALID_ARGUMENT` before anything starts. Asked again for the session it holds, it answers the same and starts nothing. Asked for another, it refuses with `FAILED_PRECONDITION`. |
| `Send`, `Follow` | Drive the session. Before `Open` they answer `FAILED_PRECONDITION`. |

A process nobody opens within five minutes exits, having touched nothing. A process that finds
something already answering on its socket exits and leaves it alone, so of two started by a host that
crashed in between, one holds the session. The answer to whether a session's start worked is in its
event log, not the answer to `Open`.

### The agent's environment

The host builds it, and this process interprets none of it. `AgentEnvironment` says what to set and
what to unset over the login shell's environment (agentshim builds a provider's environment from
that probe and never from this process's own), and each `ToolServer` carries its own whole
environment. The names, what fills each and who reads it are in
`proto/vibesys/cli/v1/conformance/identity_env.golden.json`, which the host's tests and the
vibesys-cli's tests both read.

A tool server's environment holds the session's token. It is not written to any file here, and
agentshim passes the provider its tool server configuration through an inherited pipe, so it is on no
command line either.

## When a session counts as open

`SessionOpened` is the sidecar saying the session is ready, so it is only said once the agent is
up with what it was given. `AgentshimDriver.open` starts the provider session, then asks agentshim
whether the tool servers it was given connected (`Session.report_tools`, up to `TOOLS_WAIT_SECONDS`).
Anything that fails before that ends the session at once with one `SessionFailed` and no
`SessionOpened`, carrying a `StartFailureReason` and the provider's words after "could not start
the agent:". The agent process is closed first, so nothing is left running.

| Stage | Reason | Read from |
| --- | --- | --- |
| the agent program | `AGENT_NOT_INSTALLED` | `CliNotFoundError` |
| sign-in | `NOT_SIGNED_IN` | a `TurnFailedError` of kind `AUTH` while starting |
| plugin install | `PLUGIN_UNAVAILABLE` | `PluginInstallError` |
| working directory | `WORKING_DIRECTORY_UNAVAILABLE` | an `OSError` (or its cause) naming the directory |
| tool servers | `TOOLS_UNAVAILABLE` | a server agentshim reports `FAILED` |
| anything else | `OTHER` | |

The reason comes from the type of the error and never its text. What each harness can prove about
tools: Claude Code answers `mcp_status` before the first prompt and is asked again while a server is
connecting; Codex reports each server's start-up as it happens. A server that has not settled by
`TOOLS_WAIT_SECONDS`, a provider that refuses the question, a state agentshim does not know and a
server the provider does not list are all unknown, and unknown opens the session. Neither harness reports
which plugin skills loaded, so a plugin is proven only by its install having succeeded, not by the
agent using it. An agent that is signed out but starts (the usual case) fails its first turn, not its
start. The daemon's own timer (`core.LaunchTimeout`) ends a session that never says anything.

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
  opens a new session with the `resume_token` from `SessionOpened`, which is opaque to it. A turn that
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
cannot, and the agent runs until its stdin closes, so **the daemon reaps it**. The bootstrap names
two files for this, and the sidecar uses them as follows:

- `lock_file`: the sidecar takes an exclusive `flock` on it first, before anything else, on a
  descriptor its children do not inherit, and holds it until the process ends. The system frees it
  however the process ends, so a free lock means the sidecar is gone, whatever any file says. A
  second process for the same session cannot take it and exits, whether or not the first one
  listens yet. The host holds the lock for an instant when it looks, so a busy lock is tried a few
  times (about half a second) before the process concludes that another really lives.
- `processes_file`: a `SessionProcesses` message (the sidecar's pid and the process group of each
  direct child, the agent, each with the start time of its leader as `ps -o lstart=` prints it), replaced atomically while the lock is held. It is written before the
  socket answers, checked every 0.25 s including during a turn, and rewritten when the set changes;
  a sidecar that exits on its own empties the groups after closing the agent. Where the system has
  no `/proc` that lists children (macOS), the children come from `ps -A -o pid=,ppid=`.

The daemon tries the lock to learn whether the sidecar lives. If it gets it, the sidecar is gone:
it sends SIGKILL to each group in the record (`kill(-pgid)`) whose leader is gone or started when the
record says (a leader that started at another time means the id went to another program, for
instance after a reboot, and the group is left alone and logged), removes the record, and starts the
next process, or loses the session with the reason, before it opens a resumed session on the same
worktree, so no old agent edits the worktree under the new one. If the lock is held, the sidecar
lives and the record's pid is its own, so the daemon may signal that pid and no other. A record that
cannot be read is reported with its path and ends nothing. Nothing in the sidecar reaps; the daemon
owns it.

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
  directory. `open.json` holds what `Open` carried, with the tool servers' environment left out because
  it holds the session's token. `prompts/N.md` and `spawns.json` (argv, provider home, plugin commands) record what the agent was sent and how it was launched.

`gateway/e2e/desktop_path_test.go` drives this through the desktop path (`desktop_path_e2e` check
group, CI job `desktop-path`). With `VIBESYS_E2E_DESKTOP_UI=1` it also launches the built desktop
app against the same world and checks the run list, each agent's harness and the lead's report on
screen, then presses Stop (`clients/apps/desktop/e2e`). The scripts are pure generators over a `Machine` protocol, so
`tests/test_e2e_scripts.py` runs them against a Fake without processes.
