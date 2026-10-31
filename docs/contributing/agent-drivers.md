# Agent Sessions

`AgentClient` presents one application interface over agent sessions. It owns
session reuse, skill setup, response parsing, logging, usage records, and
lifecycle. It is given a `SessionLauncher` (a `typing.Protocol` in
`vs_agent/session_launch.py`) whose `launch(spec)` returns a `LaunchedSession`
(`vs_agent/shim_turns.py`), a thin wrapper over one `agentshim.Session`. The
production launcher is `ConfinedSessionLauncher`: it owns native executor setup,
policy translation, and confinement, and the session runs turns, events, and
cleanup. All conversion between VibeSys and agentshim types lives in
`vs_agent/shim_translation.py`. Unsupported requirements fail before a session
starts.

agentshim is the only agent backend, so there is nothing to select. The former
`[agent].driver` key, which selected the removed driver layer, was removed: a
config that still sets it, including `driver = "omnigent"`, is rejected with an error naming `agent.driver`, and a
run manifest that records `execution.agent_driver = "omnigent"` is rejected the
same way. Manifests of earlier runs that recorded `agentshim` still load and
resume, and round records that carry the retired `implementer_driver` key still
load; the keys are dropped on read and new records omit them. Any other unknown
key is still rejected.

## Where agentshim lives

agentshim is a separate repository, [vic-lsh/agentshim](https://github.com/vic-lsh/agentshim),
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

VibeSys owns launch policy: which provider to run, session budgets and when to
retire a conversation, host sandbox policy, Docker lifecycle, and event
rendering. A fact that is true because of how VibeSys chooses to run agents
belongs here.

New provider behavior therefore goes upstream, not into a VibeSys
workaround.

### Import boundary

Only `vs_agent` imports `agentshim`. Everything else uses `vs_agent.api`, and
tests use `vs_agent.api.testing`. Ruff's banned-api rule (`TID251` on
`agentshim`, configured in `pyproject.toml`) enforces it, with exemptions only
for `libs/vs-agent/**` and `tests/e2e/**`. Tests of the launcher, streaming
transport, and related behavior live in `libs/vs-agent/tests`.

## Transports

A containerized session of a provider that agentshim lists in
`stream_provider_names()` (Claude Code and Codex today) keeps one long-lived
process per conversation (`TransportKind.STREAM`) instead of starting a CLI per
turn. The launcher reads that registry; it never branches on a provider name.
Every other session (Gemini, opencode, and every host session) stays one process
per turn (`TransportKind.ONE_SHOT`). `ConfinedSessionLauncher(transport=...)` fixes the
choice for tests.

A long-lived process lets a turn receive a message while it runs
(`Session.steer`) and report rate-limit windows as they change, and it keeps the
model's context between turns without a resume.

The container process is started by agentshim through
`vs_agent.docker_confinement.DockerContainerConfinement`, an
`agentshim.Confinement` over the run's `DockerSandbox`:

- `docker exec -i` with the environment passed by name, so a credential never
  appears in the host process table;
- an `AGENTSHIM_CONFINED=1` marker on every process it starts, which
  `Confinement.reap()` uses to kill them, including processes a dead host
  process left behind;
- the container id read at every call, because a GPU reselect replaces the
  container.

agentshim maps the working directory, MCP server paths and the schema directory
into the container itself, so a confined session passes host paths through.

### Startup recovery

A long-lived agent outlives a turn, so a host process that dies leaves agents
running in the run's container. A resumed run holds the run's exclusive host
lock, so the previous host is dead by then and every container labelled
`vibesys.run-id=<run id>` is an orphan. `RunEnvironment.reap_orphans` (Docker
environments; a no-op elsewhere) calls `vs_agent.reap_orphaned_agents` before the
workspace is repaired or any journal turn is reconciled: for each labelled
container it runs agentshim's `Confinement.reap()` (kills the marked agent
processes), then `docker rm -f`. A failure raises `OrphanReapError` naming the
container, and the resume stops: it must not run beside an agent that may still
be writing. After the reap, in-flight turns in the journal are reconciled by the
existing core semantics: a turn whose outcome is unknown is inspected and never
dispatched again, and a conversation the provider lost is not silently replaced
by a fresh one.

## Vetted harnesses

`vs_agent.provider_policy.harnesses()` is the one list of the provider and
model pairs VibeSys runs agents with. It is derived at call time: for each
shipped provider, the models agentshim's `ProviderProfile.models` lists, the
provider's default first. Which models a CLI runs, its default model, and each
model's context window (`agentshim.context_window`) are model knowledge, owned
by agentshim; VibeSys's policy is only which providers it ships and the
operator's opt-out. `test_no_model_knowledge_in_product_code` fails on a model
id literal in product code.

`AgentSpec` enforces it: constructing a CLI spec, or replacing its model (a
role override, a quota fallback, a chat thread), raises
`UnvettedHarnessError` unless the spec carries `allow_unvetted_model`.
`resolve_agent_specs` runs in `validate_run_request`, before any image build,
and reports the error naming the configuration key. Only the terminal's
`--allow-unvetted-model` and a resume set the opt-out. Experiment chat offers
the same list. The host helper serves it to the desktop app's start sheet
(`harnesses.list`), starts a run only with a pair from it, and refuses the
opt-out flag in a start's arguments, so a run started from the desktop app
cannot leave the list
([desktop-connection.md](desktop-connection.md#methods)).

The same construction also applies the host allow-list
(`~/.config/vibesys/host.toml`, key `allowed_models`; see
[cli-flags.md](../cli-flags.md)). A missing file or an empty list adds no
restriction. A non-empty list refuses a CLI model outside it, including on
resume, relaunch, and a chat thread, and `allow_unvetted_model` does not
bypass it. The refusal is a configuration error whose message names the key,
the role, the model, and the list, so the app shows it as a start or resume
refusal. Scripted runs and any other non-CLI backend are not checked.

## Provider readiness

A missing CLI or a logged-out account would otherwise surface as the first
turn's failure. Before a role's first session, `AgentClient` asks a launcher that
implements `ReadinessProbe` to probe its provider with `probe_readiness(spec)`.
`ConfinedSessionLauncher` calls `agentshim.probe_provider` on the executor, sandbox
confinement and environment `launch` builds for the same spec, so a
container is probed where the agent will run. No model is called.

| Probe result | Outcome |
|---|---|
| binary not found | `ProviderNotReadyError` (`BINARY_MISSING`), before any turn |
| login `FAILED` | `ProviderNotReadyError` (`AUTH_FAILED`) with the provider's own fix text |
| login `UNKNOWN` (Gemini, Copilot, opencode have no status command) | proceeds; the run log records `[readiness] ... login state unknown` |
| ready | proceeds; passing is remembered per role, a failure is not |

`ProviderNotReadyError` is permanent (`retryable = False`). A launcher with no
probe is skipped, not guessed at.

## Rate-limit reports

A provider that reports its rate-limit windows (Claude Code's `rate_limit_event`,
Codex's `account/rateLimits/updated`) reaches VibeSys as one `AgentRateLimit`
per window, carried by an `AgentEventKind.RATE_LIMIT` event. `AgentLogger`
writes a plain `[rate limit]` line to the run log and publishes the typed
`rate_limit_update` event (`RateLimitUpdateData`) for frontends. `exhausted` on
the event is resolved once, in `AgentRateLimit.is_exhausted`: the provider's own
statement wins, otherwise usage at or past 100%. Unstated values stay `None`.
The headless frontend prints a line only for an exhausted window.

## Quota and rate-limit stops

agentshim classifies why a turn failed (`FailureKind`); `vs_agent`
turns the two capacity cases into one typed error, `AgentQuotaError`
(`provider`, `condition`, `detail`, `resets_at`):

| `FailureKind` from agentshim | Also required | `QuotaCondition` |
| --- | --- | --- |
| `USAGE_LIMIT` | none | `QUOTA_EXHAUSTED` |
| `TRANSIENT` (agentshim's own waits ran out) | the provider reported an exhausted window during the turn | `RATE_LIMITED` |

An overload or server error with no exhausted window stays a plain failure, as
do `AUTH`, `OTHER` and `SCHEMA`. `resets_at` is the latest reset among the
exhausted windows the turn reported, else `None`.

`AgentClient` hands the error to a `CapacityGate` when one is installed
(`set_capacity_gate`, an optional `CapacityGated` capability) and sends the same
turn again on the same live session when the gate returns; the gate raises to end
the turn. Without a gate the error propagates. The run host installs
`PolicyCapacityGate` (`vs_runtime`) on every client it builds, through
`install_capacity_gate`, configured by `[agent.quota]`
(`QuotaPolicy`, decided by the pure `decide_quota`). Under `pause` it publishes
the typed `quota_paused` event, requests the run's cooperative pause (so the
server reports PAUSING, then PAUSED), parks until the run resumes, then
publishes `quota_resumed`. Under `wait` it parks for a bounded time (the
provider's reset time plus a margin, else `retry_seconds`, cut to what is left of
`wait_seconds`) and resumes the run itself. Under `fail`, or when the budget is
spent or the reset lies beyond it, it publishes `quota_abandoned` and raises the
quota error, so the turn fails as it did before. A stop request ends any wait.

A run fixes each role's provider when it starts (one agent client per provider),
so it cannot open a session on another provider: the run host refuses a configured
`fallback_provider` at startup, naming the key, rather than accepting and ignoring it
(`policy = "fallback"` needs one, so it is refused too). The gate and
`ProviderFallback` still implement the switch for a host whose sessions can change
provider: the gate records the replaced provider, publishes `provider_switched` and
ends the turn in flight with the quota error. A run that waited and resumed has the same experiment
state as one that never stopped, because the paused turn is the same invocation
held in place. The `quota_paused` event is the operator notification: headless
prints a `[quota]` line, and a consumer of the event stream (the event-hooks
work in #787) can act on it.

## Operator steering

An operator message waits in the run's steering queue, the single source of
truth. The core run loop drains the queue at each control poll and records every
message as a durable session input addressed to the run. The next agent turn the
run starts reserves the input, renders it into that turn's prompt, and the run
journals `steer_consumed` once the message has reached a turn. An input is
delivered exactly once: a turn that is cancelled or rejected before the provider
accepts it returns the input to pending, and a later turn receives it. A turn
already running is never interrupted by a default steer; an `--interrupt` steer is
offered to it first, as [Running VibeSys](../running-vibesys.md) describes.

## Provider session resume

MCP session identity includes its command, arguments, stable environment, and
launch-only environment key names. `vs_mcp`'s `StdioServerDescriptor.runtime_env` carries
fresh credentials and service endpoints. Its values are excluded from session
equality, fingerprints, and representations; the launcher injects them into the
MCP process on creation. Keys may not overlap the stable environment. Grant
principal, scope, role, and tool capabilities remain in the stable environment,
so credential rotation preserves continuity while authority changes reject it.

`AgentClient` keeps one live session per session key and, for keys whose scope
opts into durability, checkpoints that session's provider conversation ID in
the run's machine-local state. A resumed process offers the checkpoint to the
first session it builds for that key, so a quit run continues the
implementer's conversation instead of replaying the round.

`WorkspaceAgentSessions.create_session(member_id=..., generation=...)` names
an independent durable generation without changing the candidate workspace.
A positive generation uses `SessionScope.MEMBER_GENERATION` and
`AgentSessionKey.for_member`; omitting it preserves the stable member key.
Dynamic orchestration creates a new generation only after an explicit
continuation of a durably recorded failed evaluation resume. The old Unknown
invocation remains inspectable and fenced against replay, including after a
host restart. Initial and resumed turns share that fence in production and
in the workspace-session Fake.

Two contract members carry this:

- `AgentCapabilities.provider_session_resume` says whether a launcher's sessions can adopt a
  conversation created by an earlier process. `session_reuse` only promises
  reuse within one process.
- `LaunchedSession.adopt(session_id) -> bool` offers one
  checkpoint and returns whether it was adopted. It returns `False` when
  its provider cannot resume, or when the session already holds a live
  conversation whose history is newer than the checkpoint. A `False` answer
  tells the client the checkpoint is dead, so it drops it.

Which providers can do it is
declared by `ProviderProfile.supports_resume`, not by `vs_agent`: every CLI
VibeSys ships has a resume flag (`claude --resume <session>`,
`codex exec resume <thread>`, `gemini --resume <id>`,
`opencode run --session <id>`), so the session reports the profile's answer
rather than a hard-coded provider list.

### Restarts are reported

A turn that drops and restarts the conversation a session names sets
`AgentTurnResult.restarted`. `LaunchedSession` sets it whenever agentshim
reports a `Turn.continuity` other than `CONTINUED` (`RESET` or `REPLACED`). The
client then evicts the live session and clears the checkpoint, so nothing later
claims continuity with history that no longer exists. `agentshim.Session` owns
both restarts:

- retiring an over-budget Codex thread (turn count or heavy-turn usage),
  evaluated after the turn so the decision reads the usage it just produced;
- retrying a resumed turn once from a fresh conversation after agentshim raises
  `SessionResumeError`, which is how each provider reports that the
  conversation the turn named is gone (a missing Codex rollout, a refused
  `claude --resume`). Only a resumed turn is retried, and only once, so a
  second failure is a real agent failure and propagates.

The library restarts silently, so `LaunchedSession` logs each one (a renewed
thread, a replaced conversation, a dropped conversation) for the operator.

A turn that merely raises is not a restart. Timeouts and cancellations (a cancelled turn raises
`agentshim.TurnCancelledError` and keeps its conversation) say
nothing about whether the conversation is still resumable, so the client keeps
the checkpoint and only a reported restart (or a refused adoption) clears
it.

Journaled turns set `AgentTurnRequest.require_provider_checkpoint`, including
their first dispatch. The session retains their conversation despite its renewal
budget and never retries a refused resume in a fresh conversation. The client
requires a resumable identity and rejects actual resets or replacements. Later
starts validate the current checkpoint against the latest invocation journal;
lost or changed proof fails before provider execution. Recorded replies remain
replayable from the journal without repeating accepted work.

### A failed resumed turn drops the conversation

A resumed turn that raises a `CliExitError` whose `kind` is
`FailureKind.OTHER` still loses the conversation it was continuing: the `agentshim.Session` forgets
the conversation before re-raising, so the next turn on that session starts fresh.
A raise carries no `AgentTurnResult`, so the turn cannot report
`restarted`, and forgetting is the only way the session can refuse to
offer a conversation again.

This is a backstop, not the normal path. Codex recognizes its own
missing-rollout message and raises `SessionResumeError`; Claude, Gemini and
opencode make a refused resume indistinguishable from any other startup
failure, so agentshim maps any unclassified nonzero exit of a resumed turn
onto `SessionResumeError` for them. A failure agentshim classified
(`TRANSIENT`, `USAGE_LIMIT`, `AUTH`, `SCHEMA`) happened inside a conversation that
resumed, so it neither restarts nor drops the conversation. What is left over is a resumed turn that fails
in a way no provider calls a resume failure, and resuming that conversation
again on every later turn would make no progress. The price is that a genuine
agent failure on a resumed turn also costs that conversation's history, which
is the cheaper of the two.

The drop is session-local. `AgentClient` evicts the live session when a turn
raises and deliberately keeps the checkpoint, so a run whose provider cannot
report a refused resume can still re-adopt a dead conversation ID in the next
process. Fixing that belongs with the checkpoint, not the session.

### Transient provider errors are waited out

agentshim classifies every failed turn: `CliExitError.kind` is a
`FailureKind`, read by the provider package from what its CLI reported. `vs_agent`
never matches provider error text. A turn whose kind is `TRANSIENT`
(an overload, a rate limit, or a server error) is not retried in place:
`TRANSIENT_RETRY_DELAYS_S` is empty, because the CLI has already retried inside
the turn. The failure reaches the client as a capacity limit after one attempt,
and the run's capacity gate waits out the outage. Only the gate's waits are kept
off the turn's limit, so every retry here would charge a whole CLI attempt (3 to
4 min) to it. A retry, when a caller configures one, keeps the conversation, and
`cancel()` ends a wait immediately. A cancelled turn
still ends with its classified capacity error. Every other kind propagates at
once: a usage limit or a login problem outlasts any backoff here.

### Output that never matched its schema is a structured-response failure

A provider can give up producing output that matches the turn's response
schema (agentshim `FailureKind.SCHEMA`; Claude Code exits with
`error_max_structured_output_retries` after its own in-turn retries). `vs_agent` raises `AgentOutputSchemaError` with the provider's last validation
errors in `.detail`, and does not retry: repeating the same prompt meets the
same schema. The conversation is kept, by the session and by `AgentClient`,
which does not evict the session for this error, so a correction sent as the
next turn continues the work. vs-runtime raises it to plugins as
`StructuredResponseError` with the same `.detail`, the error an unparseable
reply raises, so every plugin handles it the same way: one correction turn,
then a recorded failed attempt or the end of the run. No plugin fabricates a
response in its place. Codex never fails this way: it constrains decoding to
the schema. `AgentClient.invoke` raises the same error, with field-named
errors (`reasoning: String should have at most 2000 characters`), for a reply
that does not validate as the response model; `FakeAgentClient` and the
vs-runtime `FakeRun` session do the same, so tests see production behavior.

### A reply that is too large or degenerate is the same failure

A model can stop making progress and keep emitting whitespace inside an open JSON
value for minutes, while the provider CLI retries the stream and the turn never
ends. `vs_agent.reply_limits` declares the bound: a reply may hold at most
`MAX_REPLY_BYTES` of text and no run of more than `MAX_WHITESPACE_RUN` whitespace
characters. The first text event that crosses a limit interrupts the turn, and the
turn raises `AgentOutputSchemaError` whose `.detail` names the breach
(`oversized_reply` or `degenerate_reply`), so the one correction turn above
handles it. A reply that arrives only as a final result is checked the same way,
and `FakeAgentClient` applies the same limits. The limits are VibeSys policy, so
they live in `vs_agent`, not agentshim.

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

## Images

A Docker run starts from two images, built by `vs_agent.api.images`:

- The **task image**, built from the task's own `Dockerfile` when it has one
  (`build_task_image`), or the backend's base image otherwise. It installs
  only what the task needs to build and test candidate code, and serves the
  evaluator as well as the agent.
- The **agent image**, built on top of the task image by `agent_image` from
  Dev Container Features under `libs/vs-agent/src/vs_agent/images/features/`.
  The `vibesys-agent` Feature installs Node, all four shipped CLIs (`claude`,
  `codex`, `gemini`, `opencode`), ripgrep, `uv`, and Python's `mcp` package,
  then creates a non-root `agent` user; the image ends with `USER agent` in
  `/workspace`. The provider a session runs is a run-time choice, so every
  shipped CLI lands in this one layer rather than one image per provider.

The agent layer sits on top so that a CLI version bump rebuilds only that top
layer, and a task image stays pure enough to serve the evaluator on its own.
Each image's local tag is keyed on its build inputs, so a daemon that already
has the tag builds nothing (a build, even a fully cached one, re-exports the
image, which takes minutes for a large base); a changed input gets a new tag
and Docker's layer cache redoes only what changed. `vs_agent.api.images`
resolves the immutable manifest ID rather than keeping a manifest of its own.

How a run's Dockerfile is generated from its Features, why no devcontainer CLI
or Docker socket is involved at run time, and how Features are tested on each
base family are in
[devcontainer-features.md](devcontainer-features.md).

CLI and toolchain versions are not in the Features' scripts: they are options
that `agent_feature` fills from the library's public API, `vs_agent.api`
(`NODE_VERSION`, `CLI_VERSIONS`; defined in the library's `provider_policy`
module). The Rust, Go, and container-runtime pins live with their Features in
`vs_sandbox.toolchain_features`, so a
version bump is a one-line change in one module. Each Feature's option defaults
repeat the pins so `devcontainer features test` can run unconfigured, and a test
fails if they drift.

The pinned Claude Code must also know every Claude model agentshim lists, or it
bills an unknown model at a fallback rate. `CLAUDE_CLI_MIN_FOR_GENERATION` names
the oldest release that knows each generation, and `test_claude_cli_pin` fails
when agentshim lists a newer generation than the table, or the pin is older
than an entry. Raise the pin in the same PR that bumps agentshim for a new
generation.

A task Dockerfile that needs the backend's base image declares `ARG
BASE_IMAGE` and `FROM ${BASE_IMAGE}`; `agent_image` always passes
`--build-arg BASE_IMAGE=<resolved base>` when building a task image, so this
is opt-in from the task Dockerfile's side. A task Dockerfile with its own
`FROM` line (the Verus task under
`examples/data-structures/repositories/queue-rs/.vibesys/tasks/verus-mpmc-open/`
is one) ignores the unused build arg and keeps its own base.

A task that declares `[environment] docker_in_docker = true` adds the `container-runtime`
toolchain to the agent layer (the `vibesys-container-runtime` Feature: a Docker
engine, the compose plugin, kind, and kubectl, pinned in `provider_policy`).
`DockerSandbox` starts such a container under Sysbox with a
`dockerd` of its own and mounts the workspace at its host path (there is no
host-socket mode); see `vs_sandbox.container_runtime` and "Docker-in-Docker" in
`docs/running-vibesys.md`.

The Features install with `apt-get`, so every task Dockerfile and every
backend base image must be Debian- or Ubuntu-derived; every current backend
base and task Dockerfile already is. Because setup happens once, at build
time, an agent cannot `apt-get install` mid-round: a missing system package
is a Dockerfile gap, not something a running turn can patch around.

The build needs only a Docker daemon the host user can run `docker build` against.
It mounts no host path into a container, so a daemon that forbids bind-mounting
`/var/run/docker.sock` (an authorization plugin on a hardened cluster host) builds
the image normally.

### Registry: GHCR by digest

A local Docker run never contacts a registry: it runs the image
`agent_image` just built straight from the local Docker image store. Modal
and SkyPilot runs do, because neither backend's Docker daemon can be assumed
to already have the image locally, so their local editor container is
started from a pushed, pulled-back reference instead of the bare local image
ID.

`vs_agent.api.images` carries the push and verification side of this:

- `push_agent_image(image_id)` tags the image as
  `ghcr.io/uw-syfi/vibesys-agent:<short id>` (a name derived from the image's
  own content address, not a moving tag), pushes it, and resolves the
  `ghcr.io/uw-syfi/vibesys-agent@sha256:...` manifest digest Docker recorded
  for that push. Remote backends reference this digest, never a tag.
- `agent_image_is_pushed(reference)` asks the registry whether a digest is
  live, via `docker manifest inspect`, without pulling any layers.
- `ensure_pushed(image_id)` is what Modal and SkyPilot actually call: it
  checks Docker's own record of where this exact image was already pushed
  before pushing again, so a repeated launch against an unchanged agent
  image after the first is a no-op past that first push. It raises
  `ImagePushError`, naming the digest, when a push fails or the registry
  does not confirm the result: the run refuses to start rather than pull an
  unverified reference.

GHCR because the repository is on GitHub and it is cloud-neutral for
SkyPilot's arbitrary infra targets. Pushing requires the Docker daemon to
already be logged in (`docker login ghcr.io` with a token carrying
`write:packages`; CI supplies this as `GITHUB_TOKEN`); `vs_agent.api.images`
performs no login of its own and never logs a credential value, only image
references and exit codes.

SkyPilot's own accelerator job is unrelated to this: the `image_id` it runs
on comes from the operator's cluster profile
(`SkyPilotProfile.remote_runtime_image`), chosen for the accuracy/benchmark
command's own runtime needs, not for running agent CLIs.

## Container execution

Docker runs the provider CLI inside the role's editor container, through
the same path a host session runs. `ConfinedSessionLauncher.launch` looks up or builds a
`vs_sandbox.WorkspaceSandbox`, wraps a plain `agentshim.HostCommandExecutor()`
through `confine_to_sandbox`, and hands `agentshim.Agent` the sandbox's own
environment; nothing in it branches on which backend it has. A container
session's sandbox is not built by the launcher: it is the run environment's
already-started `vs_sandbox.DockerSandbox`, looked up by role from the
`docker_sandboxes` dict the launcher was configured with. `sandbox.wrap` and
`sandbox.agent_path` are the only two operations the launcher calls to adapt
everything else to a container.

- `confine_to_sandbox` calls `sandbox.wrap(argv, cwd)` when the sandbox's
  `wrap` accepts a `cwd` argument (only `DockerSandbox` does, because one
  container serves every turn regardless of working directory, so the caller
  supplies it per call; a host sandbox's `wrap(argv)` fixes its own workspace
  and ignores the argument). The session's own `cwd`, passed to
  `agent.session`, is always the real host workspace path, for a host
  and a container session alike. `DockerSandbox.wrap` maps that host path to
  the container's bind mount and emits `docker exec -i -w <container path>
  ... <argv>`.
- A container session's binary lookup is overridden to trust the bare
  provider binary name to `docker exec`'s own PATH: a host-side lookup
  against the container's PATH string would search host directories the CLI
  does not live in.
- The container ID is read from the sandbox on every command, so a GPU
  reselect that replaces the container needs no new executor.
- The CLI runs as the image's `agent` user, remapped at container start to
  the host user's uid and gid, so files it writes to the bind-mounted
  workspace are owned by the host user. Nothing repairs ownership afterwards,
  and git needs no `safe.directory` entry inside the container.
- The binary health check (`<binary> --help`) runs through the same
  `confine_to_sandbox` wrapping, inside the container, once per session,
  before the first turn. See the section below for what a failure costs.
- `AgentSessionSpec.environment` is a real per-session overlay on a host
  session, folded into the environment the host sandbox is built from. It
  reaches nothing on a container session today: a container session's
  environment is `sandbox.env`, and no current caller populates `environment`
  for a containerized session (the run context's `gpu_env()`, the only source
  `AgentClient.invoke` draws it from, returns an empty mapping whenever
  `capabilities.container_execution` is true).

### A failed health check is a typed agent fault

`agentshim.Agent` runs `<binary> --help` when a session is constructed. `vs_agent`
translates failed checks, missing binaries and process execution errors into
`AgentSpawnError`, including the provider and the original cause. This aborts
the attempted turn before agent work starts. The fault is retryable: a caller's
bounded turn-fault policy can repeat setup, including after a dependency
reinstall.

A busy Docker daemon must not trip the check unnecessarily.
`ConfinedSessionLauncher(check_timeout=...)` bounds it, defaulting to 60 s in container
mode against 15 s on the host: the container check waits on `docker exec`
attaching as well as on the CLI answering.

### Session MCP servers and paths

A provider that discovers MCP servers from a config file (`claude`, `gemini`,
`opencode`) needs a directory to write it into, and agentshim derives that
directory from the session's `cwd`. That `cwd` is always the host workspace
path now, in both modes, so the config lands on the host without the launcher
naming a separate location, and a container CLI reads it back through the
bind mount at `/workspace`. Codex passes its servers as `--config` flags and
touches no workspace file either way.

Every absolute path in an MCP server's command or args, and the
response-schema directory `OutputSchema.cli_dir`, is mapped through
`sandbox.agent_path`: identity on the host, the container mount path under
Docker. A host session additionally substitutes the interpreter running
VibeSys for a bare `python` or `python3` command, because a host agent
inherits a login shell's PATH, where that name may resolve to an interpreter
without the MCP dependencies. A container session leaves the command as
written, because the image resolves its own. That distinction is one keyword
argument (`pin_interpreter`) on `_as_mcp_server`, not a container/host branch
elsewhere in the launcher.

## Usage records

Token and cost fields use JSON `null` for an unknown turn increment, including
Codex resumes whose previous cumulative total is unavailable. These fields
must not be counted as measured zero. A sum that omits unknown turns is a
lower bound, and duration remains available independently. Once agentshim
observes a resumed total, later turns report measured differences again.

`AgentClient` writes one row per invocation to `<log_dir>/usage.jsonl`, whether
or not the turn succeeded. `input_tokens` is the whole prompt the provider
billed for, cached tokens included, on every provider: agentshim folds
Anthropic's disjoint cache counts into the input total so the field means the
same thing across CLIs, and `cache_read_input_tokens` reports the cached part
separately. Records written by Claude runs before this change excluded the
cached tokens from `input_tokens`, so a Claude series that spans the change is
not comparable without adding `cache_read_input_tokens` back into the older
rows. Since agentshim 0.7, `cache_read_input_tokens` counts cache reads only;
on Claude it used to include cache writes, which `cache_creation_input_tokens`
reports.

Each row also records where the cost came from and which models spent it. `cost_basis` is how
the library reached `total_cost_usd` (`price_table`, `provider_reported` or `unknown_model`, which
has no cost; `null` when none was reported). `by_model` has one row per model the turn ran, in the
journal's names: a Claude turn's Task subagents and advisors run on their own models, and the
token fields above count only the main conversation, so the rows are a superset that sums to the
whole turn. Exactly one row is `primary`, the model the turn was run with, and only it carries the
turn's requests (`turns`) and its service `speed`. Each row's `recorded_usd` is the library's price
of the row at the standard tier, copied as given: VibeSys never recomputes a cost, and a fast or
ultrafast row was billed more than that list price. `provider_session_id` is the provider's own id
of the conversation the turn ran in, `null` when it named none or the turn failed before it did.
The run journal's `turn_usage_recorded` event carries the same three fields.

Each row also records skill use for the turn: `skill_uses` (number of skill
loads), `skills_invoked` (their names, one per load, in order) and
`skills_offered` (how many skills the provider listed for the session). A
`null` means the provider cannot report it, never zero: agentshim declares
this per provider (`ProviderProfile.skill_invocation`, `skill_discovery`).
Claude Code reports both; Codex reports loads (inferred from a shell read of a
`SKILL.md`) but not the offered list; Gemini and opencode report neither.
Which provider frames count as a load is agentshim's knowledge: `vs_agent`
maps `agentshim.SkillInvoked` to an `AgentEventKind.SKILL` event and the
turn's `agentshim.SkillSummary` to `AgentTurnResult.skills`, and matches no
tool names or paths. The offered list also appears in the run log as a
`[skills offered]` diagnostic line, and each load as `[skill] <name>`.

Every session a run starts is offered only the run's skills: the launcher asks
agentshim for `SkillScope.PROJECT`, which hides the operator's personal and
plugin skills so a run behaves the same whoever launches it. How each CLI is
told is agentshim's knowledge (`ProviderProfile.skill_scopes`). Claude Code's
mechanism also skips the operator's user settings and `~/.claude/CLAUDE.md`;
credentials still load. A provider without a mechanism (Gemini, opencode)
keeps every skill: `AgentCapabilities.skill_isolation` is false for it and the
session logs that once.

The same holds for MCP servers: the launcher asks agentshim for
`McpScope.SESSION`, so a session connects only to the servers the run
configured (the evaluation and profiler servers), not the operator's user or
project MCP configuration, plugin servers, or account connectors (claude.ai,
ChatGPT apps). The CLI-specific mechanism is agentshim's
(`ProviderProfile.mcp_scopes`). A provider without one keeps every server:
`AgentCapabilities.mcp_isolation` is false for it and the launcher logs that once
per session.

The rest of the operator's CLI configuration (user settings, hooks, global
instructions such as `~/.codex/AGENTS.md` or `~/.claude/CLAUDE.md`, notify
commands, memory) stays out through `ConfigScope.PROJECT`
(`ProviderProfile.config_scopes`). Codex can only enforce it in a state root of
its own, so a host session runs against a run-owned `CODEX_HOME` at
`Project.agent_homes_directory_for(root, run_id)/codex`, prepared by
`agentshim.prepare_config_home`. One home per run and provider, so a
conversation resumes across candidates. The home's `auth.json` is a symlink to
the operator's: Codex rotates its refresh token and writes `auth.json` in
place, so a copy would log out whichever side did not refresh. For the same
reason the sandbox grants the auth file read-write. A provider without a
mechanism (Copilot, Gemini, opencode), a container session, or a launcher built
without an agent-homes directory keeps `ALL`:
`AgentCapabilities.config_isolation` is false and the launcher logs it per
session. Managed policy settings and the workspace's own `.claude/` or
`.codex/` configuration still apply.

A host session also inherits only an allowlisted part of the launcher's
environment (`vs_agent.session_environment`): `PATH`, `HOME`, user, shell,
`TMPDIR`, `TZ`, `TERM`, locale (`LANG`, `LANGUAGE`, `LC_*`), proxy and CA
bundle variables, `CARGO_HOME`, `RUSTUP_HOME`, `DOCKER_HOST`, VibeSys's own
sandbox controls, and the selected provider's credential and state-root
variables (`ProviderProfile.auth_env_vars`, `state_root_env`). The run's own
variables are added on top. An operator adds names with `[agent]
env_passthrough = ["NAME", ...]`; an entry that is not a variable name is
rejected when the config loads. `CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`
and `ROCR_VISIBLE_DEVICES` are allowlisted so an operator's GPU pin reaches the
agents. The launcher logs once per run, at the start of the first session, the
names (never values) of launcher variables it did not pass.

Variables operators commonly need to add:

| Need | Names |
| --- | --- |
| Shared libraries and CUDA toolkit | `LD_LIBRARY_PATH`, `CUDA_HOME`, `CUDA_PATH` |
| Hugging Face | `HF_TOKEN`, `HF_HOME` |
| Python package indexes and uv | `PIP_INDEX_URL`, `UV_*` names such as `UV_INDEX_URL`, `UV_CACHE_DIR` (list each name) |
| Claude on Bedrock | `CLAUDE_CODE_USE_BEDROCK`, `AWS_*` names such as `AWS_REGION`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` |
| Claude on Vertex | `CLAUDE_CODE_USE_VERTEX`, `CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID` |

`env_passthrough` takes exact names, not patterns.

## FakeProvider

`FakeProvider` is test infrastructure, exposed through `vs_agent.api.testing`
(defined in `vs_agent/scripted_provider.py`). It opens scripted sessions that
stream an explicitly scripted turn, so tests exercise the real `AgentClient` ->
run-owned `CoreAgentEventSink` -> `EventJournal` -> server integration ->
transport path without an agent CLI, a model, or a network. It never writes
events, state, or files itself. There is no config key that selects it.

`FakeProvider` takes an explicit `turn=[...]` (or `turns=[[...], ...]` for a
sequence of distinct turns) of `agentshim` events, plus `answer=` for a
structured reply and `script=` (`FakeTurnScript`) for per-turn answers and
scheduled conversation resets. A response schema with no scripted answer
raises rather than being fabricated.

Tests that need typed policy replies compose `FakeAgentClient` through the
public test session factory. Fault-injection wrappers `FaultingExecutor` and
`FaultingTransport` also live in `vs_agent` and are re-exported from
`vs_agent.api.testing`.

## Sandboxing

Vocabulary, ownership, configuration flow, and the support matrix are in
[Sandboxing and confinement](sandboxing.md). This section covers the agentshim
launcher only.

`ConfinedSessionLauncher` applies VibeSys confinement as an executor transform:
`confine_to_sandbox` rewrites every command's argv through the confinement's
`wrap`, unconditionally, the single chokepoint through which the provider CLI
is launched on the host or in a container alike. The provider CLIs run with
approvals and their own sandboxes off, so this is the only boundary.

Provider state comes from `ProviderProfile.state_dirs` and is granted whole,
because a CLI writes session history and caches there and needs them back on
resume. A provider whose profile declares `resume_state_paths` is the exception:
its state directory may hold checkouts (a Codex checkout can live under
`$CODEX_HOME/worktrees`), so only its authentication files (read-write) and
those paths are granted. The history paths are not optional: a rollout that does
not outlive its turn makes a resume report no rollout for the thread, and a
confined run then loses the conversation continuity it was told it had.

## End-to-end tests

`tests/e2e/test_confined_session_e2e.py` drives `ConfinedSessionLauncher` against the
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
