# Agent Drivers

`AgentClient` presents one application interface over the supported drivers. It
owns session reuse, skill setup, response parsing, logging, usage records, and
lifecycle. A driver owns native executor setup, policy translation, turns,
events, and cleanup. Unsupported requirements fail before a session starts.

Omitting `driver` selects `agentshim`. Select the Omnigent driver directly:

```toml
[agent]
backend = "cli"
driver = "omnigent"
```

The `omnigent` package is a base dependency (pinned exactly in
`pyproject.toml`), so every install carries it; `uv sync` is enough.

## Where agentshim lives

agentshim is a separate repository, <https://github.com/vic-lsh/agentshim>,
published to PyPI as `agentshim`. VibeSys depends on it like any other package
and pins the version in `pyproject.toml`.

To develop against a library commit that has no release yet, add a
`[tool.uv.sources]` override:

```toml
[tool.uv.sources]
agentshim = { git = "https://github.com/vic-lsh/agentshim", rev = "<sha>" }
# or, for a local checkout:
# agentshim = { path = "../agentshim", editable = true }
```

Revert to the PyPI pin before merging: a branch that keeps the override builds
only where that checkout or commit exists.

### Who owns what

agentshim owns provider knowledge: argv construction, stream parsing, MCP
config file formats, output-schema dialects, provider state directories, auth
environment variables, skill directories, install recipes, and resume flags. A
fact that is true because of how a CLI behaves belongs there.

VibeSys owns driver policy: which provider to run, session budgets and when to
retire a conversation, host sandbox policy, Docker lifecycle, and event
rendering. A fact that is true because of how VibeSys chooses to run agents
belongs here.

New provider behavior therefore goes upstream, not into a VibeSys driver
workaround.

## Provider session resume

`AgentClient` keeps one live session per session key and, for keys whose scope
opts into durability, checkpoints that session's provider conversation ID in
the run's machine-local state. A resumed process offers the checkpoint to the
first session it builds for that key, so a quit run continues the
implementer's conversation instead of replaying the round.

Two contract members carry this:

- `AgentCapabilities.provider_session_resume` says whether a driver can adopt a
  conversation created by an earlier process. `session_reuse` only promises
  reuse within one process.
- `AgentSession.resume_provider_session(session_id) -> bool` offers one
  checkpoint and returns whether it was adopted. A driver returns `False` when
  its provider cannot resume, or when the session already holds a live
  conversation whose history is newer than the checkpoint. A `False` answer
  tells the client the checkpoint is dead, so it drops it.

Cross-process resume is AgentShim-only today. Which providers can do it is
declared by `ProviderProfile.supports_resume`, not by the driver: every CLI
VibeSys ships has a resume flag (`claude --resume <session>`,
`codex exec resume <thread>`, `gemini --resume <id>`,
`opencode run --session <id>`), so the driver reports the profile's answer
rather than a hard-coded provider list. Omnigent 0.10 owns its executor's
conversation lifecycle internally and exposes no attach point, so
`OmnigentSession` reports `provider_session_resume=False` and always refuses.

### Drivers must report restarts

A driver that drops and restarts the conversation a session names must return
`SessionDisposition.RESET_REQUIRED` on that turn's `AgentTurnResult`. The client
then evicts the live session and clears the checkpoint, so nothing later claims
continuity with history that no longer exists. The AgentShim session reports a
reset for both of its restarts:

- retiring an over-budget Codex thread (turn count or heavy-turn usage),
  evaluated after the turn so the decision reads the usage it just produced;
- retrying a resumed turn once from a fresh conversation after agentshim raises
  `SessionResumeError`, which is how each provider reports that the
  conversation the turn named is gone (a missing Codex rollout, a refused
  `claude --resume`). Only a resumed turn is retried, and only once, so a
  second failure is a real agent failure and propagates.

A turn that merely raises is not a restart. Timeouts and cancellations say
nothing about whether the conversation is still resumable, so the client keeps
the checkpoint and only a driver-reported reset (or a refused adoption) clears
it.

### A failed resumed turn drops the conversation

A resumed turn that raises anything other than `SessionResumeError` still
loses the conversation it was continuing: the AgentShim session calls
`forget()` before re-raising, so the next turn on that session starts fresh.
A raise carries no `AgentTurnResult`, so the turn cannot report
`RESET_REQUIRED`, and forgetting is the only way the session can refuse to
offer a conversation again.

This is a backstop, not the normal path. Codex recognizes its own
missing-rollout message and raises `SessionResumeError`; Claude, Gemini and
opencode make a refused resume indistinguishable from any other startup
failure, so agentshim maps any nonzero exit of a resumed turn onto
`SessionResumeError` for them. What is left over is a resumed turn that fails
in a way no provider calls a resume failure, and resuming that conversation
again on every later turn would make no progress. The price is that a genuine
agent failure on a resumed turn also costs that conversation's history, which
is the cheaper of the two.

The drop is session-local. `AgentClient` evicts the live session when a turn
raises and deliberately keeps the checkpoint, so a run whose provider cannot
report a refused resume can still re-adopt a dead conversation ID in the next
process. Fixing that belongs with the checkpoint, not the driver.

### Retired after a turn, or replaced during one

A reset says the conversation is gone, not when it went. The two cases differ
for any caller that shortens a prompt because a conversation already carries
its instructions, so `AgentClient` answers both questions separately:

- `provider_session_id(key)` names the conversation the **next** turn
  continues, or `None` when the next turn starts from nothing. A reset clears
  it.
- `last_turn_provider_session_id(key)` names the conversation the **last
  completed** turn ran in. A reset does not clear it.

An over-budget Codex thread is retired after it has answered, so the two
disagree only from the next turn onward: that answer stands, and the next
prompt is a cold one. A conversation the provider refuses to resume is replaced
mid-turn, so the last turn ran somewhere the caller did not intend, and a
caller that shortened its prompt has to ask again in full. The
experiment chat is the caller that does this today
(`src/server/chat/session.py`).

## Container execution

`--docker` runs the provider CLI inside the role's editor container. The
driver keeps a `DockerCommandExecutor` that rewrites every agentshim command
into `docker exec -i -w <workdir> [-e ...] <container> <argv>`, so the library
still builds argv, parses the stream, and owns the session.

- The container ID is read from the sandbox on every command, so a GPU
  reselect that replaces the container needs no new executor.
- A container turn names no working directory of its own. `-w` carries it, and
  the executor's default is `/workspace`.
- `AgentSessionSpec.environment` reaches the CLI through `TurnRequest.env`,
  which the executor turns into `-e` flags. Which variables cross is declared
  by the driver, not inferred: a container starts from its image's
  environment, and the environment agentshim assembles for a turn describes
  the host. Forwarding by inspection would point the container CLI at host
  paths and could carry a host `ANTHROPIC_MODEL` past the container's own
  configuration.
- After every container turn the driver repairs workspace ownership. CLI
  agents run as root in the editor container, and an atomic file replacement
  leaves the replacement owned by root on a bind-mounted workspace.
- The binary health check (`<binary> --help`) runs inside the container, once
  per session, before the first turn. See the section below for what a failure
  costs.
- Codex gets a `CodexRolloutWatchdogExecutor` in front of the transport. A
  resumed `codex exec --json` inside a container regularly finishes its work,
  writes the terminal events to its rollout file, and never exits; the
  watchdog reads the rollout, replays the completion into the stream, and
  stops the process. It is provider-behaviour compensation and stays in
  VibeSys until the behaviour is verified fixed upstream.

### A failed health check ends the run

`CliAgent` runs `<binary> --help` when a session is constructed, and raises
`CliCheckError` when it fails. Nothing in the loop catches that: session
construction failures propagate out of the round, so a container whose CLI is
missing, unauthenticated, or unreachable stops the run instead of burning a
turn budget discovering it. That is the intended behavior; the check exists
precisely so the failure is cheap and legible.

Because a failure is that expensive, the check must not be tripped by a busy
Docker daemon. `AgentShimDriver(check_timeout=...)` bounds it, defaulting to
60 s in container mode against 15 s on the host: the container check waits on
`docker exec` attaching as well as on the CLI answering.

Follow-up, not implemented: the loop could treat a session construction
failure as fail-closed evidence about the round (the same way it treats an
agent timeout) rather than letting it escape as an unclassified error. That
would give the operator a diagnostic naming the container and the provider
instead of a bare `CliCheckError`.

### Session MCP servers in a container

A provider that discovers MCP servers from a config file (`claude`, `gemini`,
`opencode`) needs a directory to write it into, and agentshim derives that
directory from the turn's working directory. A container turn has none, so it
names the host workspace in `TurnRequest.mcp_workspace` instead: the library
writes the config there for the turn and removes it afterwards, and the CLI
reads it through the bind mount at `/workspace`. Codex passes its servers as
`--config` flags and touches no workspace file either way.

The MCP command is left as the caller wrote it in container mode. Only a host
turn rewrites a bare `python` to the interpreter running VibeSys, because the
container image resolves its own.

## Usage records

`AgentClient` writes one row per invocation to `<log_dir>/usage.jsonl`, whether
or not the turn succeeded. `input_tokens` is the whole prompt the provider
billed for, cached tokens included, on every provider: agentshim folds
Anthropic's disjoint cache counts into the input total so the field means the
same thing across CLIs, and `cache_read_input_tokens` reports the cached part
separately. Records written by Claude runs before this change excluded the
cached tokens from `input_tokens`, so a Claude series that spans the change is
not comparable without adding `cache_read_input_tokens` back into the older
rows.

## Mock driver

`driver = "mock"` is test infrastructure. It satisfies the same driver
contract while streaming a deterministic playbook, so tests exercise the real
`AgentClient` -> `OutputSink` -> server integration -> transport path without an agent
CLI, a model, or a network. It never writes events, state, or files itself.

```toml
[agent]
backend = "cli"
driver = "mock"
```

Two playbooks, both in `vibesys.agents.drivers.mock`:

- `ScriptedPlaybook` synthesizes a turn from configurable counts: assistant
  text chunks, thinking chunks, tool call/result pairs of a chosen payload
  size, todo snapshots, and usage updates, with optional per-event pacing.
- `ReplayPlaybook` re-emits a recorded run's `run-events.jsonl` at a
  configurable speed (`0` replays as fast as the consumer accepts events).

Structured turns are answered from `vibesys.agents.scripted_rounds`, which the
stub agent client shares, so a scripted run completes loop rounds on the happy
path. A response schema with no scripted artifact raises rather than being
faked. The mock is not offered through the client protocol: driver choice
stays an implementation detail.

## Omnigent constraints

- Only the `claude` and `codex` providers are supported. Omnigent 0.10.0 has no
  Gemini harness, and its `opencode-native` executor cannot run a headless
  VibeSys turn.
- `--docker` is rejected because the integration has no container launcher.
- Session-scoped stdio MCP servers use Omnigent's native MCP manager. VibeSys
  translates its provider-independent server declarations into Omnigent
  `MCPServerConfig` values, discovers namespaced tools before the first turn,
  and owns each session's MCP connections and subprocess cleanup.
  These generated specs declare no Omnigent guardrails; VibeSys remains the
  authority for which session-scoped servers are supplied to each role.
  Omnigent 0.10 launches stdio MCP subprocesses directly as children of the
  VibeSys process, outside the agent's OS-tool sandbox. Session MCP specs must
  therefore remain trusted framework configuration, not candidate input. With
  an explicit server `env`, the subprocess inherits the VibeSys process
  environment after Omnigent removes runner authentication secrets, then
  overlays those values. Without an explicit `env`, Omnigent delegates to the
  MCP SDK's restricted default environment.
- Extra host resource grants are rejected. The Omnigent path imports only the
  installed Rust toolchain automatically.
- Hidden project paths become explicit Omnigent masks. Read-only declarations
  are accepted only for top-level dot paths such as `.git` and `.vibesys`.
  Those paths are protected by the agent contract, not sandbox enforcement.

## Sandboxing

The agentshim driver applies its `vs_sandbox` host sandbox as an executor
transform: `confine_to_sandbox` wraps every command that names a working
directory, which is the single chokepoint through which the provider CLI is
launched. A command without a working directory (the binary health check, and
a container-executed turn) is left alone. The
Omnigent driver builds an `OSEnvSpec` that grants workspace write access and
narrow read access to the active Rust toolchain. It selects bubblewrap on Linux
or Seatbelt on macOS and never permits an unconfined fallback.

Provider state comes from `ProviderProfile.state_dirs` and is granted whole,
because a CLI writes session history and caches there and needs them back on
resume. Codex is the exception: a Codex checkout may itself live under
`$CODEX_HOME/worktrees`, so only named leaves are granted (`auth.json`,
`config.toml`, `sessions`). `sessions` is not optional: a rollout that does
not outlive its turn makes `codex exec resume` report no rollout for the
thread, and a confined run then loses the conversation continuity it was told
it had.

VibeSys exposes only the Rust sysroot's `bin`, `lib`, and optional `libexec`
trees. Each executor gets an ephemeral writable Cargo home, removed when the
executor closes. Cargo keeps its conventional workspace `target` directory.
Declared hidden paths and `.codex-tmp` are explicitly masked. Top-level dot-path
scanning fails if it exceeds Omnigent's limit instead of silently exposing
paths.

Omnigent 0.10.0 cannot make `.git` and `.vibesys` read-only beneath a writable
workspace. Local operational state therefore lives outside the repository by
default, and the run contract protects those directories. This has not been
proven equivalent to sandbox enforcement.

Omnigent routes file and shell access through its `sys_os_*` tools. The driver
builds and dispatches those tools, currently through Omnigent's private
`_tool_executor` attribute. Codex native filesystem tools are disabled so all
file and shell operations use this sandboxed path.

The host must provide `bwrap` on Linux or `sandbox-exec` on macOS. If it is
missing, the driver raises `OmnigentDriverError` instead of running unconfined.
GitHub's Linux runners do not provide `bwrap`, so real OS-environment tests skip
there unless `VIBESYS_REQUIRE_SANDBOX_TESTS` is enabled.

Automated tests cover provider wiring, sandbox construction, tool dispatch,
event handling, and teardown. Credentialed live CLI validation is outside the
repository test suite.

## End-to-end tests

`tests/e2e/test_agentshim_driver_e2e.py` drives `AgentShimDriver` against the
installed `claude` and `codex` binaries on the host path: one turn, a resumed
second turn, a structured turn, and a session-scoped stdio MCP server
(`tests/support/mcp_add_server.py`). Every case is skipped unless
`VIBESYS_E2E_AGENTS=1` and the provider binary is on PATH, so an ordinary
`uv run pytest` needs no credentials and makes no network calls.

```bash
VIBESYS_E2E_AGENTS=1 uv run pytest tests/e2e -q -p no:cacheprovider -s
```

`-s` is worth passing: each case prints what the CLI actually returned. The
tests are marked `e2e`, so `-m e2e` selects them and `-m "not e2e"` excludes
them even when the environment variable is set. The agent environment has
`CLAUDECODE` removed, because an outer Claude Code session exports it and a
nested CLI behaves differently with it set.
