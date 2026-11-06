# Orchestrator

Remote client verbs require `vibesys-host` on the remote PATH. Install that name
on the remote before connecting; there is no fallback to the former host name.
Desktop provisioning installs it beside old executables and leaves them untouched,
since existing agent sessions may still name the old executable in their MCP configuration.

The orchestrator is a Go daemon that runs on the host doing the work, usually a remote one. It
launches Claude Code and Codex agents, records everything they do, relays typed messages between
them, and wakes a lead agent when a subagent or a job finishes. It sits beside the Python runs and
shares nothing with them: it has its own contract under `proto/vibesys/orchestrator/v1/`, its own
module here, and it reaches `vibesys-cli` only through that tool's public contract.

Work is landing in stages. This module holds the contract, the pure decision core, the interface
over `vibesys-cli` jobs, the code that converts between the two, the store, the daemon with its
live hub, the MCP server, a headless client and the sidecar launcher, all hosted by `vibesys-host`. The
plan reader is a placeholder in production and the job source is left unwired until their items land
(`internal/production`); both say so when asked.

## Vocabulary

- **Run:** one optimization of one task. It keeps its id across restarts.
- **Agent:** one agent in a tree under the run's lead. It keeps its id across resumes.
- **Session:** one process of an agent. Each resume is a new session with a higher number.
- **Sidecar:** the process that holds a session. It outlives the daemon, so a daemon restart does
  not stop an agent.
- **Turn:** one unit of work by an agent, started by one message.
- **Item:** one ordered entry in a turn, changed in place from running to finished.
- **Message:** typed mail between agents and the operator: handoffs, reports, steering.
- **Effect:** something the core asks the outside world to do, kept in a durable outbox until done.

## Layers

| Package | Holds | May import |
| --- | --- | --- |
| `internal/core` | State, `Decide`, the role table. No I/O, clock or randomness | Nothing of this module |
| `internal/jobs` | `JobSource`, the interface over `vibesys-cli` jobs | `core` |
| `internal/jobs/jobstest` | The in-memory `JobSource` and the contract every one passes | `core`, `jobs` |
| `internal/wire` | Bytes to core types and back, once, at the edge | `core`, `gen` |
| `internal/localfs` | Proof that the state directory is on a local filesystem | Nothing of this module |
| `internal/store` | The durable record: atomic commits, recovery, outbox, paged reads, retention | `core`, `wire`, `localfs` |
| `internal/store/storetest` | The contract every store passes, a workload from the real core, and a model | `core`, `store`, `wire` |
| `internal/hub` | Live deltas to the clients watching an agent: merged, bounded, never stored | `core`, `gen` |
| `internal/daemon` | The engine, effect worker, sidecar streams and `OrchestratorService`. No clock, no real I/O | `core`, `store`, `jobs`, `wire`, `hub`, `gen` |
| `internal/daemon/daemontest` | The Fakes and the `Rig` that wire a whole daemon in memory | `daemon` and what it imports |
| `internal/client` | The client library, `connect` and the headless commands, over the public service API | `gen` only |
| `internal/mcp` | The stdio MCP server an agent talks to, over a small `Daemon` interface the client satisfies | `core`, `gen` |
| `internal/production` | The one place that reads the real clock, sockets and files | `client`, `core`, `daemon`, `hub`, `jobs`, `localfs`, `store`, `gen` |
| `hosted` | The orchestrator as `vibesys-host` hosts it, and its `mcp`, `run` and `send` verbs. The one package other modules import | `client`, `core`, `daemon`, `mcp`, `production`, `gen` |
| `gen/` | Generated from the `.proto` files. Do not edit | Nothing of this module |

`internal/archcheck` enforces this table and the purity of the core from the import graph of every
package, along with the rule that this module imports no other module of the repository.

## How a decision is made

Everything that happens to the system is one input to one function:

```go
func Decide(s State, in Input) (State, []Event, []Effect)
```

An input is a command, a sidecar fact, a job update or a timer firing. It carries its time and a
nonce, so the function reads no clock and mints ids with no randomness. The shell commits the events
and the effects in one transaction, then carries out the effects and feeds what comes of them back
in as inputs.

Every state change goes through one fold over events, so a state rebuilt from the log with `Replay`
equals the live one by construction, and the simulation checks that it does.

What this gives the daemon to come:

- A command is answered once. A retry with the same id gets the same receipt, and the same id on a
  different command is a conflict. A refusal is remembered as well, across a restart.
- An effect id is stable, so a repeat from the outbox is recognised and dropped by whoever carries
  it out. Delivery is at least once, with no duplicates seen by the agent.
- A sidecar fact counts once. A stream resumed from the last committed offset changes nothing it
  already changed, and a fact from a session that has been replaced changes nothing at all.
- No wait is open forever. A session that does not answer a stop is written off by a timer, a
  subagent that stops without a handoff tells its lead, and retries are bounded.

## The store

`internal/store` makes each `Decide` result durable as one transaction: the events, the tables the
screens read, the receipt of a command, and the effects still to carry out, with events written
before effects. The events are the truth. A restart loads the checkpoint and replays the events
after it with `core.Replay`, and `Verify` checks that every table equals what that replay gives.

Two stores pass one contract suite: `Memory`, the Fake the daemon's tests use, and `SQLite`, one
file in a state directory with a write-ahead log, `synchronous = NORMAL` and one writer. The driver
is pure Go, so the daemon cross-compiles with no C toolchain.

The state directory comes from `VIBESYS_ORCHESTRATOR_STATE_DIR`, or defaults to
`$XDG_STATE_HOME/vibesys/orchestrator` and then `~/.local/state/vibesys/orchestrator`. SQLite cannot
lock safely on NFS, SMB and their kin, so `localfs` follows symlinks and refuses such a directory
before the database opens anything. The check fails closed: only filesystem types known to be local
pass (a list for Linux and for macOS), a network type is refused and named, and so is a type nobody
listed, with its statfs number. There is no override: the error says to point the variable at a local
path, and a default on a network filesystem is refused and never replaced by another place. An empty
home directory is an error, not a default relative to the working directory.

The gateway that hosts the orchestrator is found through its root, and the root follows the state
directory: without `--root`, `vibesys-host serve`, `connect` and the client verbs (`run`, `send`, `mcp`)
use `$VIBESYS_ORCHESTRATOR_STATE_DIR/host` when the variable is set, and `~/.vibesys` when it is not.
So `run start` with the variable set reaches, or starts, the gateway of that state directory and
never one that serves another; `--root` overrides both. The rule is `hosted.GatewayRoot`.

Writes never block reads, and the long ones never block the writer for long. `Verify` and the
read-heavy part of `Compact` run on a snapshot beside the writer. `Compact` writes in steps of at
most `StepRows` rows (1000 by default), each its own short transaction, and a crash between steps
leaves a store that recovers the same state. Retention covers the event log and the checkpoint only:
a finished run's events go, and its agents, turns, items and messages stay readable (see
`Retention`). A reader whose cursor is above the end of the log gets `ErrAheadOfLog`, because a
power cut under `synchronous = NORMAL` can leave the log shorter than a sequence a client already
saw; the daemon answers with a fresh snapshot.

The crash tests fail or kill the store at every statement and every COMMIT of a commit, taken at the
database driver (`store.Op`) and not at points the store names, and require that each result is
whole, that every returned commit survives, and that the outbox holds exactly the effects of the
events that did. Moving the outbox writes into a second transaction fails them.

`ORCHESTRATOR_STORE_SCALE=1000000 go test ./internal/store -run TestMeasureScale -v` measures recovery
time and read latency on generated data.

## The daemon and its commands

`vibesys-host` hosts the daemon (`orchestrator/hosted`): `vibesys-host serve` registers `OrchestratorService`
beside `HostService` on the gateway's socket and serves the agent-only socket itself. One `vibesys-host`
holds the state directory through the store's lock; any other answers orchestrator calls
`UNAVAILABLE` naming the holder's pid and version, and keeps serving `HostService`. It does not exit
for being idle while a run is live. It is crash-only: starting is recovering. A serial loop feeds commands, sidecar events, job polls and timers to `Decide`, commits
the result through the store, and then a worker carries out the effects at least once. Time and
nonces are injected, and an input that changes nothing is logged.

```sh
vibesys-host serve                                       # the gateway, with the orchestrator
ssh host vibesys-host connect                            # the one client transport: stdio to the socket
vibesys-host mcp                                         # stdio MCP server; identity comes from the environment
vibesys-host run start --repository R --task T --brief B --harness claude_code
vibesys-host run watch                                   # a snapshot, then events, as JSON lines
vibesys-host run stop RUN
vibesys-host send AGENT "text"
```

The daemon ends the sidecar process of every session the core no longer counts as live, whatever ended
it: a close the sidecar reported, a failure, a stop nobody answered, or a resume that replaced the
session. So when `run stop` returns and the run is over, none of its sidecars is left, the lead's
included. The sidecar's own linger (a minute after it closes) does not serve a stop: it is for a
daemon that is down or restarting, which reads the close when it reattaches, and it is the backstop if
the daemon never gets to end the process. Before the daemon did this, a closed sidecar exited only when
its linger ran out, and the sidecar of a session that failed without closing never exited. A kill that
fails is tried again by the next step; a daemon that stopped before it made the kill ends the sidecars
of the sessions it finds over when it starts (`Reattach`). A process id is only signalled when the
process's command line carries the session's directory, so a number the system has given to another
program is never killed.

A snapshot holds runs and agents, and every turn and message of those runs, not items. A watch opened
after an agent replied does not show that reply; `run watch --after SEQUENCE` replays the log after a
sequence, such as the one in a command's receipt. A run with more turns than a timeline window holds
shows its earliest in the snapshot.

## Running it

Set `VIBESYS_ORCHESTRATOR_STATE_DIR` to a local-disk directory and `VIBESYS_ORCHESTRATOR_SIDECAR`
to an `orchestrator/sidecar` checkout (or the command that runs it), and `VIBESYS_ORCHESTRATOR_MARKETPLACE`
to the checkout of the plugin marketplace (the repository root, which holds `.claude-plugin/marketplace.json`);
there is no default for any of them. Every session, lead and subagent, on either harness, loads the
`vibesys` plugin from that checkout and none of the operator's own plugins, skills or configuration.
The plugin is installed once per agent into `STATE/sessions/AGENT/plugin-home`, which a resume reuses.
A missing or wrong marketplace refuses every launch, naming the setting. Build with
`go build -o ~/.local/bin/vibesys-host ./gateway/cmd/vibesys-host`, run `uv sync --frozen --project
orchestrator/sidecar` once, then `vibesys-host run start ...` starts a gateway if none serves. Each
session's files, logs and socket are under `STATE/sessions/`.

The lead's first message is rendered by the sidecar from the run's task, its `--fanout` and the
optional `--brief`, the operator's steering text: it tells the lead to run `vibesys:optimize` for the
task, so the repository must hold `.vibesys/tasks/TASK/`.

### Which `vibesys-cli` the agents run, and where it finds its daemon

An agent's shell may be a login shell (Codex runs `bash -lc`), which reads the user's profile again
and can put another `vibesys-cli` first on `PATH`. So the daemon picks one build when it starts. It
picks, in order: `VIBESYS_ORCHESTRATOR_CLI` (an absolute path to an executable file; a relative path
or a file that is not executable stops the orchestrator from starting, naming the setting, and no
other build is substituted), a `vibesys-cli` beside the `vibesys-host` binary, then the first
`vibesys-cli` on the daemon's own `PATH`. With none of them the orchestrator does not start, because
the skills have no `PATH` fallback: they run `"$VIBESYS_CLI"` and stop with a clear message when it
is unset.

The choice is recorded, with the places that build reads its daemon and store from
(`XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `HOME` of the daemon's own environment, each only if it is an
absolute path), as the run's `CliLocation` when the run starts. Every session of the run, a resumed
one after a daemon restart included, is launched with the run's recorded location and its task, so
the agents of a run meet one daemon and one store. The sidecar puts them in the agent's environment
with the run's identity (`VIBESYS_REPO`, `VIBESYS_TASK`, `VIBESYS_RUN`, `VIBESYS_AGENT`,
`VIBESYS_HARNESS`, `VIBESYS_SESSION`), as its README describes. The sidecar process's own
environment carries none of it: agentshim builds a provider's environment from a login-shell probe,
so only the environment the sidecar gives the `Agent` arrives.

Live smoke run with Claude Code and Haiku 4.5 (it uses a real account, so an operator runs it; the
repository needs the task bundle named by `--task`):

```sh
export VIBESYS_ORCHESTRATOR_STATE_DIR=/local/disk/vo-smoke VIBESYS_ORCHESTRATOR_SIDECAR=$PWD/orchestrator/sidecar VIBESYS_ORCHESTRATOR_MARKETPLACE=$PWD
go build -o ~/.local/bin/vibesys-host ./gateway/cmd/vibesys-host && uv sync --frozen --project orchestrator/sidecar
git init -q /local/disk/smoke-repo && printf 'def add(a, b):\n    return a - b\n' > /local/disk/smoke-repo/calc.py
git -C /local/disk/smoke-repo add -A && git -C /local/disk/smoke-repo commit -qm init
vibesys-host run start --repository /local/disk/smoke-repo --task fix-add --harness claude_code \
  --model claude-haiku-4-5 --fanout 2 \
  --brief 'calc.py has a bug in add; keep the run to one worker and one judge.'
vibesys-host run watch --after 1 # in another shell; stop with `vibesys-host run stop RUN` when the lead reports
```

### Who may call

The daemon serves two sockets, and which one a caller reached decides who it is:

- The **operator socket** takes calls with no credentials and refuses any call that carries an
  agent's. It is the gateway socket that `vibesys-host run`, `send` and `connect` use, and it is never put in
  an agent's environment.
- The **agent socket** is the only socket a launched session is told (`VIBESYS_ORCHESTRATOR_SOCKET`).
  Every call on it needs the credentials of the agent's current session (agent id, session number
  and an HMAC token the daemon derives from a secret), and the daemon never reads the caller from a
  request body. An agent may issue the commands its role allows and read the snapshot of its run.
  It cannot read history or plans.

An agent sees every agent's items of its own run through `WatchRuns`: that is intended, since a lead
and its workers work from one picture of the run.

The limit, stated plainly: agents run as the same Unix user as the daemon. A hostile process of that
user can read the operator socket's path from the state directory, or the token secret from the file
beside the database, and act as the operator. The split fences an agent's mistakes and a stale
session, and makes the operator socket something no tool is pointed at. Holding a determined agent
back is a matter of agent confinement and run environments, in `docs/contributing/sandboxing.md`.
An operator token file was rejected: a same-user agent can read it too, so it adds a secret and no
boundary.

### Restarts, calls to the world and failures

- A restart first carries out every effect the last daemon left in the outbox, and only then looks
  for the sidecars of the other live sessions. A session whose launch was committed and not carried
  out is therefore launched, and is never mistaken for one whose sidecar died.
- Every call to a sidecar, the job source or the plan reader runs under a bound from an injected
  source (30 seconds in production). A call that outlives it is retried, and the retry blocks the
  loop for at most one bound, so a sidecar that stopped answering delays the others by that much
  per attempt and loses nothing.
- A failed effect holds back the later effects of its session in the same batch, so a stop never
  reaches a session before the turn it stops. Outbox rows that the store could not settle are
  settled by the next step.
- A session that could not be launched or found carries the reason in the agent's `end_detail`, and
  a subagent's lead is told it in the settled message. With no job source wired, the daemon asks
  none; it does not pretend there are no jobs.
- If either server or the loop stops, the daemon exits with the error, so it never stays up
  serving nothing.

Watching gives a snapshot and then the events after its sequence. A client too far behind gets a
fresh snapshot, never a gap. Live deltas of a running item reach only the clients watching that
agent, merge within 50 ms, are bounded per client, and are always superseded by the stored item of
the same sequence.

## Install for managed runs

Status: the installer and the release workflow exist, but no release has been published yet, so the
command below does not work until the first `vibesys-v*` tag is pushed.

```bash
curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/install.sh | sh
vibesys-client run start --task <task>
```

`install.sh` (POSIX sh, no root, no shell-profile edits) downloads the bundle for your operating
system and architecture, checks its SHA-256, and lays it out like this:

```text
~/.vibesys/releases/<version>/bin/{vibesys-client,vibesys-host,vibesys-cli}
~/.vibesys/releases/<version>/sidecar/       sidecar source and lock, plus a .venv made by uv
~/.vibesys/releases/<version>/marketplace/   the skills plugin marketplace
~/.vibesys/current -> releases/<version>
~/.local/bin/vibesys-client -> ~/.vibesys/current/bin/vibesys-client
```

Only `vibesys-client` goes on your PATH. The host finds the sidecar, the marketplace and
`vibesys-cli` relative to its own binary. If `uv` is missing the installer installs it into
`~/.vibesys/uv`; uv fetches Python 3.12 itself. Git must be installed, because the sidecar depends
on agentshim by Git URL.

- A failed step names itself and leaves the previous install working: `current` moves last.
- Installing the same version again changes nothing.
- Switching versions is refused while a `vibesys-host` started from a release under `~/.vibesys`
  is running (found in the `ps` command lines); `--force` overrides. Old releases are kept.
- `VIBESYS_VERSION`, `VIBESYS_INSTALL_DIR`, `VIBESYS_HOME`, `VIBESYS_RELEASES_URL` and
  `VIBESYS_INSTALL_BUNDLE=/path/to/bundle.tar.gz` (offline install) are read; see the header of
  `install.sh`.

You also need Claude Code or Codex installed and logged in, and Docker for tasks that use it.
Agents run as your user, unconfined, with permissions bypassed.

Bundles are built by `scripts/build_release_bundle.sh VERSION OS ARCH OUTDIR` and published by
`.github/workflows/managed-release.yml` on `vibesys-v<version>` tags, with attestations; the tag
`vibesys-latest` names the newest stable version. `scripts/tests/test_install.sh` installs a bundle
in a clean container.

## Testing

`./check.sh` runs formatting, `go vet` and every test.

The core is tested at three levels, all in one thread with no real time:

- `decide_test.go` names one scenario per behavior.
- `simulation_test.go` runs whole runs against a model of everything around the core: a daemon that
  crashes and replays its log through stored bytes, an outbox that repeats effects, sidecars that
  outlive the daemon or die on their own, clients that retry, and a job source that repeats itself.
- `monitor_test.go` checks the guarantees from the committed log, with its own books, so it shares
  no code with the core: no agent runs two turns at once, fan-out stays within its limit, each
  message is taken exactly once, the replayed state equals the live one, and every run and message
  ends when everything stops. It also counts what each simulation reached, and fails a property that
  was never exercised.

One seed fixes a whole simulation. A failure prints the seed and the command that replays it:

```sh
ORCHESTRATOR_SIM_SEED=123 go test ./internal/core -run 'TestSimulation$'
```

To search further than the default 40 seeds:

```sh
ORCHESTRATOR_SIM_SEEDS=400 go test ./internal/core -run 'TestSimulation$' -timeout 10m
```

`internal/daemon/sim_test.go` runs a whole daemon (engine, effect worker, sidecar streams, job poll,
service and hub) over Fakes from one seed, while it kills and restarts the daemon, kills sidecars,
repeats and conflicts commands, refuses bad credentials, fails launches and job reads, leaves
clients that stop reading, makes sidecar calls hang until their bound ends, makes the store fail to
settle rows, and ends the process inside a step (after the claim, after a command was sent, before
the rows are completed). Besides the core's invariants it checks two promises: every effect
committed for a session that is still live reaches its sidecar, and a restart alone never ends a
live run or writes off a live agent. Replay one seed or search further the same way:

```sh
ORCHESTRATOR_SIM_SEED=7 go test ./internal/daemon -run 'TestDaemonSimulation$'
ORCHESTRATOR_SIM_SEEDS=600 go test ./internal/daemon -run 'TestDaemonSimulation$'
```

`internal/client` drives a whole run from a client, and connects through a real pipe that fragments,
delays and cuts. `internal/wire` builds random messages from the `.proto` descriptors, so a field added to the
contract is tested with no change to the tests, and a mapper that forgets it fails its round trip.
Its fuzz targets take untrusted bytes; `go test` runs their seeds, and this runs one for real:

```sh
go test ./internal/wire -run '^$' -fuzz '^FuzzParseCommand$' -fuzztime 30s
```

## Generation

The code in `gen/` comes from `proto/vibesys/orchestrator/v1/` through `buf.gen.orchestrator.yaml`,
and `scripts/generate_proto.sh` regenerates it with the rest of the repository's generated code.
