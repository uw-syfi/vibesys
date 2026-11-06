# Orchestrator

The orchestrator is a Go daemon that runs on the host doing the work, usually a remote one. It
launches Claude Code and Codex agents, records everything they do, relays typed messages between
them, and wakes a lead agent when a subagent or a job finishes. It sits beside the Python runs and
shares nothing with them: it has its own contract under `proto/vibesys/orchestrator/v1/`, its own
module here, and it reaches `vibesys-cli` only through that tool's public contract.

Work is landing in stages. This module holds the contract, the pure decision core, the interface
over `vibesys-cli` jobs, the code that converts between the two, the store, the daemon with its
live hub, the MCP server and the sidecar launcher, all hosted by `vibesys-host`, and the verbs of `vibesys-client`. The
plan reader is a placeholder in production and the job source is left unwired until their items land
(`internal/production`); both say so when asked.

## Vocabulary

- **Run:** one optimization of one task. It keeps its id across restarts.
- **Agent:** one agent in a tree under the run's lead. It keeps its id across resumes.
- **Session:** one process of an agent. Each resume is a new session with a higher number.
- **Sidecar:** the process that holds a session (`AgentSessionService`). It outlives the daemon, so a
  daemon restart does not stop an agent. Nothing in the contract names it.
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
| `internal/prompts` | The words an agent is sent for a message: Go templates, goldens from the Python renderer they replaced | `core` |
| `internal/localfs` | Proof that the state directory is on a local filesystem | Nothing of this module |
| `internal/store` | The durable record: atomic commits, recovery, outbox, paged reads, retention | `core`, `wire`, `localfs` |
| `internal/store/storetest` | The contract every store passes, a workload from the real core, and a model | `core`, `store`, `wire` |
| `internal/hub` | Live deltas to the clients watching an agent: merged, bounded, never stored | `core`, `gen` |
| `internal/daemon` | The engine, effect worker, sidecar streams and `OrchestratorService`. No clock, no real I/O | `core`, `store`, `jobs`, `wire`, `hub`, `gen` |
| `internal/daemon/daemontest` | The Fakes and the `Rig` that wire a whole daemon in memory | `daemon` and what it imports |
| `internal/client` | The client library and `connect`, over the public service API | `gen` only |
| `clientcli` | The verbs of `vibesys-client`: `run start`, `list`, `attach`, `get`, `wait`, `steer`, `stop`. Holds no run state; every real input (terminal, clock, signals, the way to the host) arrives in its `Env`. One of two packages, with `gatewayroot`, that `vibesys-client` imports | `client`, `gen` |
| `gatewayroot` | Which host `.vibesys` directory a gateway serves from, so the host and its clients agree on it | `localfs` |
| `internal/mcp` | The stdio MCP server an agent talks to, over a small `Daemon` interface the client satisfies | `core`, `gen` |
| `internal/production` | The one place that reads the real clock, sockets and files | `client`, `core`, `daemon`, `hub`, `jobs`, `localfs`, `store`, `gen` |
| `hosted` | The orchestrator as `vibesys-host` hosts it, and its `mcp` verb. The one package the host imports | `client`, `core`, `daemon`, `mcp`, `production`, `gen` |
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
- No wait is open forever. A session that does not open, or does not answer a stop, is written off by a timer, a
  subagent that stops without a handoff tells its lead, and retries are bounded.

## The store

`internal/store` makes each `Decide` result durable as one transaction: the events, the tables the
screens read, the receipt of a command, and the effects still to carry out, with events written
before effects. The events are the truth. A restart loads the checkpoint and replays the events
after it with `core.Replay`, and `Verify` checks that every table equals what that replay gives.

Two stores pass one contract suite: `Memory`, the Fake the daemon's tests use, and `SQLite`, one
file in a state directory with a write-ahead log, `synchronous = NORMAL` and one writer. The driver
is pure Go, so the daemon cross-compiles with no C toolchain.

The state directory is the host's `state_directory` setting (see [the host's root](#the-hosts-root)).
For the default root it otherwise defaults to `$XDG_STATE_HOME/vibesys/orchestrator` and then
`~/.local/state/vibesys/orchestrator`, and for any other root to `ROOT/state`. SQLite cannot
lock safely on NFS, SMB and their kin, so `localfs` follows symlinks and refuses such a directory
before the database opens anything. The check fails closed: only filesystem types known to be local
pass (a list for Linux and for macOS), a network type is refused and named, and so is a type nobody
listed, with its statfs number. There is no override: the error says to choose a local path with
`vibesys-client host configure --state-directory PATH`, and a default on a network filesystem is refused and never replaced by another place. An empty
home directory is an error, not a default relative to the working directory.

### The host's root

A root is the directory a host serves from: one socket, one lock, one log, one configuration and one
state directory. The installer, the host and every client use one rule, `gatewayroot.Rule`:

> the root is `--root` when given, else `VIBESYS_HOME` when set, else the directory this program is
> installed in (the one holding `releases/`), else `~/.vibesys`; `VIBESYS_HOME` must name the directory
> this program is installed in.

So an installed client always reaches the host of its own install. A `VIBESYS_HOME` that names another
place is an error that prints the rule (exit 2), never a guess.

The host's configuration is the file `ROOT/host-config.json`, a `HostConfig` message. No file means
every default. `vibesys-client host configure` writes it and, with no flag, prints it:

| Flag | Setting | Default |
| --- | --- | --- |
| `--state-directory` | where the orchestrator keeps its state | follows the root, as above |
| `--session-holder` | an `orchestrator/sidecar` checkout, or the command that runs it | the release's |
| `--skills-directory` | a checkout holding `.claude-plugin/marketplace.json` | the release's |
| `--job-tool` | an absolute path to an executable `vibesys-cli` | the release's |
| `--tool-path` | the directories the host looks for programs in, separated by colons | `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin` |

A host takes nothing else from the environment it was started in. It clears it and keeps `HOME`, a
`PATH` from the tool path, `USER`, `LOGNAME`, `SHELL` and `TMPDIR`, which say who and where the session
is, and `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `VIBESYS_CLI_CONFIG`,
which the job tool resolves its daemon, store and operator configuration from, so the host meets the
operator's own daemon and a daemon the host starts obeys the operator's Slurm and Docker settings. `VIBESYS_ORCHESTRATOR_SIDECAR`,
`_MARKETPLACE`, `_STATE_DIR` and `_CLI` are no longer read, and a client that starts a host with one
of them set names it on stderr with the flag that replaces it.

A client first asks what serves the root and acts on the answer, one action per case
(`clientcli.Decide`, pure, and held over every combination by a property test). A read is every run
verb but `start`:

| What is there | read | `run start` | `host restart` | `host stop` |
| --- | --- | --- | --- | --- |
| nothing | start a host | start a host | start a host | nothing to do |
| a host on another machine, through a shared disk | 4, naming it | 4, naming it | 4, naming it | 4, naming it, and never signals it |
| a host that does not answer | 4, naming `--force` | 4, naming `--force` | 4, naming `--force` | 4, and `--force` ends it |
| serving: this client's own, on this configuration | use | use | replace unless runs are live | stop |
| serving: older, or another build | use, with a note | replace when idle, else 1 | replace unless runs are live | stop |
| serving: this client's own, on another configuration | use, with a note | replace when idle, else 1 | replace unless runs are live | stop |
| serving: newer | use, with a note | use; on another configuration 1, naming `host restart` | replace unless runs are live | stop |
| not serving: state held by another process | 4, naming it | 4, naming it | 4, naming it | stop |
| not serving: that process has gone, or the state was refused | 4, naming the way out | replace when idle, else 1 | replace unless runs are live | stop |

Idle means no live run and no other client connected. A host of a release is ordered by the host's
own `relation`; a build of no release is another build unless it is the very program this client would
start, and cannot be ordered. A newer or older host whose protocol does not match fails the handshake,
which names `host restart`.

Replacing proves first that a new host can start: the program is found, the configuration reads, the
state directory is usable and the release's parts verify. Nothing has been stopped when that fails.
A `host-config.json` that does not read does not get in the way of a host that is running: verbs go on
with a warning naming the file and the key, and `host status` says so. A verb that must start a host
refuses, saying the same. `host configure --clear` never reads the file, and `host configure` refuses
what it can tell no host could use.

A root on a shared disk is seen from other machines, whose socket connections are refused and whose
process numbers mean something else. The lock's record names the machine, so that is a finding of its
own, whatever the verb. A shared disk whose locks are not shared between machines cannot be told apart
from no host at all.

Every call to a host has a bound: 5 s for `HostControlService`, 10 s for the handshake, and 30 s for a
command, a snapshot and the start of a stream, which then lasts as long as the run. An expired call
exits 3, or 4 for `HostControlService`, naming what to do.

A host stops answering a little before it has exited. It lets go of the state directory first and its
root's lock last, but a killed process drops both in an order nothing here controls, so a new host
that holds the root's lock and finds the state held by a host of the same root waits on that lock, for
up to 5 s, then serves or refuses. A holder that names another root is refused at once. The state
directory has its own lock because `--state-directory` can point two roots at one store.

A client never kills a live run. A start that a host refuses leaves no host behind, and the message is
what the host reported for this attempt, not a line from an old log. Usage errors exit 2.

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
holds the state directory through the store's lock. Any other refuses to start, naming the holder's pid
and version, unless it was started with `--orchestrator-optional`, as the desktop app's bridge
(`vibesys-host connect`) does: that one stays up, answers orchestrator calls `UNAVAILABLE` and keeps
serving `HostService`. Desktop provisioning installs `vibesys-host` beside old executables and leaves
them untouched, since existing agent sessions may still name the old executable in their MCP
configuration. A host does not exit for being idle while a run is live. It is crash-only: starting is recovering. A serial loop feeds commands, sidecar events, job polls and timers to `Decide`, commits
the result through the store, and then a worker carries out the effects at least once. Time and
nonces are injected, and an input that changes nothing is logged.

```sh
vibesys-host serve                                       # the gateway, with the orchestrator
ssh host vibesys-host connect                            # stdio to the gateway's socket, for the desktop app
vibesys-host mcp                                         # stdio MCP server; identity comes from the environment
```

`vibesys-client` is what people and babysitting agents use, as `docker` is to `dockerd`. It holds no run
state: every verb is calls on `OrchestratorService`. It finds the `vibesys-host` beside itself (symlinks
followed, so `~/.local/bin/vibesys-client` finds the release directory's host), else on `PATH`, and starts
`vibesys-host serve --root ROOT` when none serves.

```sh
vibesys-client run start --task T [--repository DIR] [--brief TEXT] [--harness claude_code|codex] [--model M] [--allow-agent HARNESS[:MODEL]]... [--detach] [--json]
vibesys-client run list                       # every run, newest first
vibesys-client run attach [RUN]               # follow a run; Ctrl-C detaches and never stops it
vibesys-client run get [RUN] [--json]         # status, agents, cost, checks, best, last report
vibesys-client run wait [RUN] --until started|report|progress|stalled|ended --timeout 30m [--after N]
vibesys-client run steer [RUN] "text" [--agent AGENT]
vibesys-client run stop [RUN] [--yes]
```

A run spends only on agents its operator chose. It may start subagents on its lead's harness and model, and
on each `--allow-agent` (a harness alone allows that harness's default model). A spawn that names neither
takes the lead's harness and the first allowed model on it; a spawn outside the allowance gets
`REFUSAL_AGENT_NOT_ALLOWED` with the list, and starts nothing. `Run.allowed_agents` holds the allowance and
`run get` prints it as "may use". `run get` also counts the agents that ended without a measured cost, since
their spend is missing from the usage total.

When a run is over (and never before), the orchestrator removes the working copy of each subagent that holds
nothing git lacks. A commit made there that no branch holds is first kept under
`refs/vibesys/runs/<run>/<agent>` in the repository (`git log refs/vibesys/runs/<run>/<agent>`), so
`git gc` does not prune it; if that ref cannot be written the copy stays. A copy with modified or untracked files is
kept, and `Run.kept_checkouts` names it, which `run get` prints as "kept". The lead works in the repository
itself and has no copy to remove.

A report is a `progress` note or a `final` report, numbered from 1 within its run. `wait --until report`
returns for a final report, at once with the one the run already has however late the wait begins, and
`--until progress` for any report, counting those posted after the wait began (an ended run counts the one it
has). `--after N` counts only reports numbered above N, for either. A run is stalled when its lead is idle
and no agent, job or message is outstanding: `--until stalled` returns then, `run get` says how long, and the
orchestrator reminds the lead after 10 idle minutes (at most 3 times per run). A lead that has given a final report and
has had no new input since is finished, not stalled: it is not reminded, and `run get` says so. A wait for a report on a
stalled run goes on to its timeout, which names the stall.

`vibesys-host` always serves `HostControlService` (`Identify`, `Stop`), whether or not it can serve the
orchestrator, and `vibesys-client host` is its client. It never starts a host to ask a question:

```sh
vibesys-client host status [--json]                      # release, relation to this client, live runs, each part and why not, who holds the state
vibesys-client host stop [--if-no-live-runs] [--force]   # exit 0 stopped or none running, 1 refused (live runs), 4 no answer or on another machine
vibesys-client host restart                              # stop an idle host and start one from this client's release
```

`--if-no-live-runs` makes the host refuse, and name how many, when a run is live. `--force` ends the
process named by the host lock when the host does not answer, and never one on another machine. The
lock, not a process list, says who holds the host: the host writes a typed `HostLock` record (its pid,
release, root and machine) into the lock file it holds, and a client reads it only while the lock is held. A `restart`
with live runs is refused for now. The release layout is resolved on every launch and start check, so
repairing an install under a running host needs no restart. The home is resolved through symlinks, so a
home reached by either path is one host.

`RUN` is a run id, a unique prefix, or omitted for the one live run in the current repository. With no run
live there, an omitted `RUN` means the repository's most recent run to `run get`, `run attach` (which
replays it) and `run stop` (which says it has already ended and exits 0, as it does for any `RUN` that has). The
repository of a new run is the root of the repository around the working directory unless `--repository`
says otherwise. Exit codes: 0 ok, 1 failed or refused, 2 usage, 3 timeout, 4 service unavailable. With no
terminal nothing prompts. `run start` follows the stream until the lead is live and exits 1, with the lead's
own detail and the start failure reason, when the run died first. `run get --json` is the service's `RunsSnapshot` filtered to one run,
with its `searches` as the snapshot carried them; a snapshot cut to fit says `truncated`, and the human
summary then omits the counts it cannot stand behind.

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

### How a session starts: one outcome

Starting a session has one outcome, within a time limit: it opens, or it fails with a typed reason.
The rest of the system does not care how the session is held.

```
start session  ->  opened | failed(StartFailureReason, detail)
```

A session is not live when its process answers. The core treats it as live (`SessionOpened`)
only when its sidecar says it is, and the sidecar says so only once the agent is up, in its working
directory, with the plugin installed and the tool servers it was given connected. The tools check is
the provider's own answer, asked of agentshim before the first turn (`Session.report_tools`): a tool
server it reports failed ends the start as `TOOLS_UNAVAILABLE`. What a provider does not say, or says in
words nobody knows, is unknown, and unknown opens the session: a run never fails on missing evidence.
Claude Code is asked with `mcp_status` and Codex's own start-up reports are read, so both can answer
before the first prompt.

Starting one crosses to the sidecar in three calls, which only `internal/production` knows: find or
start the process, `Hello`, and `Open`, which carries everything in one validated message and is safe
to repeat. The daemon sees a session that started and then opened or failed with a reason, and the
core sees none of it. A host that crashes between any two of these finds the process again and asks
again; one process holds a session, and it starts one agent.

A store of another schema version is refused, with what to do: this contract changed the stored
state, and there is no upgrade. A session of another protocol found after an upgrade is ended, and
its agent resumes under this host's.

The reason is the closed enum `StartFailureReason` on `Agent.start_failure_reason` and on the sidecar's
`SessionFailed`, beside the free-text `end_detail`, which keeps the provider's own words and always
begins "could not start the agent:".

| Reason | What failed | Who knows |
| --- | --- | --- |
| `AGENT_NOT_INSTALLED` | the agent program is not on the host | sidecar, from the type of the error |
| `NOT_SIGNED_IN` | the agent refused its sign-in while starting | sidecar, only when the agent fails at start; an agent that starts and is refused on its first request is a failed turn |
| `PLUGIN_UNAVAILABLE` | the plugin could not be installed | sidecar |
| `TOOLS_UNAVAILABLE` | a tool server of the session failed to connect | sidecar, from the provider's report |
| `WORKING_DIRECTORY_UNAVAILABLE` | the agent's directory could not be made or entered | the daemon (a worktree that cannot be created), or the sidecar |
| `TIMED_OUT` | the session did not open within `core.LaunchTimeout` | the core |
| `INCOMPATIBLE_VERSIONS` | the process holding the session and this host share no protocol (`Hello`); it is ended, and nothing was opened on it | the host |
| `STATE_LOCATION_TOO_LONG` | the place the host keeps sessions in is too long for a session's socket (`maxSocketPath` bytes); no session can start there | the host |
| `OTHER` | none of these, including a process that could not be started or reached, and a start refused by a field's name | the daemon or the sidecar |

The time limit is a timer the core arms through `Decide` with every `LaunchSession` (`TimerLaunchDeadline`,
an `ArmTimer` effect like the run's deadline), so it is injected and fired like any other and survives a
restart. `LaunchTimeout` is 5 minutes: longer than the bounds inside a launch added together (plugin
install, two commands of 60 s each; the agent's start, 60 s; the wait for tools, 30 s; the sidecar's
start, 30 s), so a launch that is only slow ends with its own reason and the timer is the backstop for
one that ends with none. When it fires on a session still launching the session fails as `TIMED_OUT` and
the existing path ends its process. A launch whose session ended before the daemon carried it out looks
for the process it may have started and ends that too. A restart of the daemon during a launch changes
none of this: the session opens, fails with its reason, or is written off by the timer.

A snapshot holds runs and agents, and every turn and message of those runs, not items, so `run attach`
shows an agent's turns and the handoffs and reports a late watcher missed, and not what the agent said
in them (`WatchAgent` and `ListItems` page items). A run with more turns than a timeline window holds
shows its earliest in the snapshot.

## Running it

The host finds its parts from the release folder it is installed in, so an installed host needs no
setting. A release says what it holds in `RELEASE/release.json`, a `ReleaseManifest`
(`proto/vibesys/orchestrator/v1/release.proto`) that the builder writes through
`orchestrator/cmd/release-manifest` and that the host, the client and the installer read through
package `orchestrator/release`. That package is the one place the layout is written down; the
archchecks refuse another spelling of `release.json` or `.venv` in product code. Today a release
holds:

```
RELEASE/release.json        the manifest
RELEASE/bin/                vibesys-client  vibesys-host  vibesys-cli
RELEASE/sidecar/            pyproject.toml, uv.lock, src/, .venv/   (.venv made at install by uv sync --frozen)
RELEASE/marketplace/        .claude-plugin/marketplace.json, skills/...
```

`vibesys-client release verify [--installed] DIR` checks DIR against its own manifest, names every
file that is missing or cannot run, prints the version on success, and needs no host. The installer
asks it, instead of keeping a path list. `--installed` also requires what install makes, the session
holder's environment.

The manifest's session holder command is `sidecar/.venv/bin/python -m vs_sidecar`, so `uv` is not
needed at run time. Every session, lead and subagent, on either harness, loads the `vibesys` plugin from
`RELEASE/marketplace` and none of the operator's own plugins, skills or configuration. The plugin is
installed once per agent into `STATE/sessions/AGENT/plugin-home`, which a resume reuses.

Each part is chosen, in order, from the host's configuration, then the release, then an error:

| Part | Configuration (used as given, errors included) | Release |
| --- | --- | --- |
| sidecar | `--session-holder` | the manifest's session holder command |
| marketplace | `--skills-directory` | the manifest's skills folder |
| `vibesys-cli` | `--job-tool` (see below) | the manifest's job tool |

RELEASE is the folder that holds the running `vibesys-host`'s own folder, when it holds a
`release.json`. A configured part that is wrong is an error and is never replaced by the release. A
developer's build is in no release: a host built into `~/.local/bin` has no `release.json` above it,
so with no configuration it refuses every launch with an error naming the flag and the manifest it
looked for. A release with a part missing is an error naming that file. `vibesys-client` finds its
host the same way, then on `PATH` for a build that is in no release.

For a developer checkout: build with `(cd gateway && go build -o ~/.local/bin/vibesys-host ./cmd/vibesys-host && go build -o ~/.local/bin/vibesys-client ./cmd/vibesys-client)`,
run `uv sync --frozen --project orchestrator/sidecar` once, run
`vibesys-client host configure --session-holder $PWD/orchestrator/sidecar --skills-directory $PWD`, then
`vibesys-client run start ...` starts a gateway if none serves. Each session's files, logs and socket are
under `STATE/sessions/`.

To start a run and leave it, use `run start --detach`, then come back with `run attach`, `run get`,
`run wait` and `run stop`. Two things outside this program can end a run when you log out. On a
systemd machine, `KillUserProcesses=yes` (in `logind.conf`, and the default in some distributions)
kills a user's processes when the last login ends, the host and the agents included; this is not solved
here: run the host under `systemd-run --user` with lingering on (`loginctl enable-linger USER`), a
terminal multiplexer, or set `KillUserProcesses=no`. And `vibesys-cli`'s own daemon keeps its socket in
`XDG_RUNTIME_DIR` (see its README), which agents use for measurement: without lingering that directory
goes with the last login, so a run's measurements fail after you log out. Enable lingering for runs that
outlive your session.

The lead's first message is rendered by the sidecar from the run's task, its `--fanout` and the
optional `--brief`, the operator's steering text: it tells the lead to run `vibesys:optimize` for the
task, so the repository must hold `.vibesys/tasks/TASK/`. A start that cannot work is refused when it
is dispatched, for every client, and makes no run, event or receipt: `InvalidArgument` when the
repository path is not absolute, does not exist, is not the root of a git work tree (it has no
`.git`), has no `.vibesys/tasks/TASK/` (a task name is one folder name, so `../x` is refused), has no
commit yet, or does not hold the task in its latest commit; and `FailedPrecondition` when the host
has no way to start agent sessions, with its own reason. The last two are because subagents work in
copies of the repository made from its latest commit (`git worktree add --detach`), so a repository
with no commit, or whose task is only in the working tree, would fail at the first `spawn_agent` or
leave them without the task. Edits to the task's files that are not committed are accepted here: the
`optimize` skill stops the lead when tracked files are modified, and that rule is not repeated. A
detached HEAD is accepted. The message names the offending path and what to do. A retry of a command
id that already has a receipt gets that receipt without the check. The check is `daemon.StartChecks`,
implemented by `production.StartChecks`; only the operator's `StartRun` is checked. It also returns the repository
with its symbolic links resolved, and the service starts the run in that form, so one directory is one
repository whichever name a client used (the command's digest stays that of what the client sent, so a
retry is the same command). `vibesys-client` resolves links the same way when it compares the working
directory with a run's repository (`Env.Canonical`).

**One live run per repository.** The lead works in the repository itself, so two runs would share one
git checkout. `Decide` refuses a `StartRun` while any run that is not over (`starting`, `running` or
`stopping`) has the same repository, whatever its task, with `REFUSAL_REPOSITORY_IN_USE`; the
receipt's `run_id` is that run (the oldest, if an old log holds more than one), and `vibesys-client`
prints it with how to attach or stop it. The rule is in the pure core, so it holds for every client and
is a simulation invariant. It is checked only when a start is decided, not by `State.Violations` or
replay, so a log written before the rule that holds two live runs in one repository replays as it was
written and those runs can still be stopped.

### Which `vibesys-cli` the agents run, and where it finds its daemon

An agent's shell may be a login shell (Codex runs `bash -lc`), which reads the user's profile again
and can put another `vibesys-cli` first on `PATH`. So the daemon picks one build when it starts. It
picks, in order: the configured `--job-tool` (an absolute path to an executable file; a relative path
or a file that is not executable stops the orchestrator from starting, naming the flag, and no
other build is substituted), the release's job tool (an error naming the file when the release lacks
it, never another build), then, for a host in no release, the first `vibesys-cli` on the daemon's own
`PATH`. With none of them the orchestrator does not start, because
the skills have no `PATH` fallback: they run `"$VIBESYS_CLI"` and stop with a clear message when it
is unset.

When a run starts the host asks that tool, in the host's own environment: `daemon ensure --json`
makes sure of a daemon and answers with its socket and its runtime directory, so the host holds no
rule for where it is. A start the tool cannot answer is refused, in the tool's words: for one, a
daemon of another build that has jobs. The program and the directory are recorded as the run's
`JobToolEndpoint`. Every session of the run, a resumed one after a daemon restart included, is
launched with the run's recorded endpoint and its task, so the agents of a run meet one daemon and
one store. The host builds them into the session's `AgentEnvironment` with the run's identity
(`VIBESYS_REPO`, `VIBESYS_TASK`, `VIBESYS_RUN`, `VIBESYS_AGENT`, `VIBESYS_HARNESS`), the program
(`VIBESYS_CLI`) and the directory (`VIBESYS_CLI_RUNTIME_DIR`), and `Open` carries it. Nothing else of
the operator's environment changes: an agent's `HOME` and `XDG_*` are the operator's, so its
credentials and git configuration resolve as for a person. The sidecar process's
own environment carries none of it: agentshim builds a provider's environment from a login-shell
probe, so only the environment the sidecar gives the `Agent` arrives.

Live smoke run with Claude Code and Haiku 4.5 (it uses a real account, so an operator runs it; the
repository needs the task bundle named by `--task`):

```sh
(cd gateway && go build -o ~/.local/bin/vibesys-host ./cmd/vibesys-host && go build -o ~/.local/bin/vibesys-client ./cmd/vibesys-client) && uv sync --frozen --project orchestrator/sidecar
vibesys-client host configure --state-directory /local/disk/vo-smoke --session-holder $PWD/orchestrator/sidecar --skills-directory $PWD
git init -q /local/disk/smoke-repo && printf 'def add(a, b):\n    return a - b\n' > /local/disk/smoke-repo/calc.py
git -C /local/disk/smoke-repo add -A && git -C /local/disk/smoke-repo commit -qm init
vibesys-client run start --repository /local/disk/smoke-repo --task fix-add --harness claude_code \
  --model claude-haiku-4-5 --fanout 2 --detach \
  --brief 'calc.py has a bug in add; keep the run to one worker and one judge.'
vibesys-client run wait --until report --timeout 30m   # from /local/disk/smoke-repo; then `vibesys-client run stop --yes`
```

### Who may call

The daemon serves two sockets, and which one a caller reached decides who it is:

- The **operator socket** takes calls with no credentials and refuses any call that carries an
  agent's. It is the gateway socket that `vibesys-client` and `vibesys-host connect` use, and it is never put in
  an agent's environment.
- The **agent socket** is the only socket a launched session is told (`VIBESYS_ORCHESTRATOR_SOCKET`).
  It is `agent.sock` in the state directory, not in `XDG_RUNTIME_DIR`: on a systemd machine without
  lingering, the runtime directory (`/run/user/UID`) is removed when the user's last login ends, and a
  run that was left running would lose its tools' socket. A state directory whose path makes the socket
  address too long for a Unix socket (100 bytes here) is an error naming
  `vibesys-client host configure --state-directory`. The sidecar sockets are under `STATE/sessions` and
  the gateway socket is `ROOT/host.sock`, or for a long root one under `/tmp/vibesys-UID`, so none of
  ours is in the runtime directory.
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
`~/.vibesys/uv`; uv fetches Python 3.12 itself. The installer needs no git; starting a run does,
because a task lives in a git repository.

After the install a run needs no setting. Start one, leave it, check it and stop it:

```bash
vibesys-client run start --task <task> --detach   # in the task's repository
vibesys-client run wait --until report --timeout 30m
vibesys-client run get          # or: run list
vibesys-client run stop --yes
```

Every binary in the bundle reports the bundle's version: `vibesys-client --version`,
`vibesys-host --version` and `vibesys-cli --version`.

- A failed step names itself and leaves the previous install working: `current` moves last.
- Installing the same version again changes nothing.
- Switching versions asks a running host to stop, through the new release's own client
  (`vibesys-client host stop --if-no-live-runs`, read by exit code): an idle host stops, a host with
  live runs, one that does not answer or one on another machine refuses the switch, having said what to
  do. `--force` skips the question. Old
  releases are kept.
- With no version named, the installer reads the newest stable version from `vibesys-latest`. While
  only pre-releases are published that tag does not exist, and the installer says to pass
  `VIBESYS_VERSION=<version>`.
- `VIBESYS_VERSION`, `VIBESYS_INSTALL_DIR`, `VIBESYS_HOME`, `VIBESYS_RELEASES_URL` and
  `VIBESYS_INSTALL_BUNDLE=/path/to/bundle.tar.gz` (offline install) are read; see the header of
  `install.sh`.

You also need Claude Code or Codex installed and logged in, and Docker for tasks that use it.
Agents run as your user, unconfined, with permissions bypassed.

Bundles are built by `scripts/build_release_bundle.sh VERSION OS ARCH OUTDIR` and published by
`.github/workflows/managed-release.yml` on `vibesys-v<version>` tags, with attestations; the tag
`vibesys-latest` names the newest stable version. `scripts/tests/test_install.sh` builds two real
bundles and, in a clean container as an ordinary user, installs them and runs the installed product:
a start with no harness installed fails naming the missing harness (the release found its own
sidecar), and a run with scripted agents (`sidecar/e2e`, selected by
`vibesys-client host configure --session-holder`, no model called) is started, waited on, listed, stopped and shown to
leave no process behind.

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
