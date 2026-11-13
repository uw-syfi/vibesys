# Orchestrator

The orchestrator is a Go daemon that runs on the host doing the work, usually a remote one. It
launches Claude Code and Codex agents, records everything they do, relays typed messages between
them, and wakes a lead agent when a subagent or a job finishes. It sits beside the Python runs and
shares nothing with them: it has its own contract under `proto/vibesys/orchestrator/v1/`, its own
module here, and it reaches `vibesys-cli` only through that tool's public contract.

This module holds the contract, the pure decision core, the interface over `vibesys-cli` jobs, the
code that converts between the two, the store, the daemon with its live hub, the MCP server and the
sidecar launcher, all hosted by `vibesys-host`, and the verbs of `vibesys-client`. Production follows
the CLI task stream into one reconciled search store, opens a fresh task stream for each check-in,
and uses the reconciled store as the level-triggered job source (`internal/production`).

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
| `internal/cost` | What a run has cost, worked out from its finished turns: the `Ledger` and its `View`. No I/O, clock or randomness | `core` |
| `internal/window` | What happened in a run between two times (`Facts`): spend by type of agent, agents started and ended, measurements, the best of each objective at both ends, plans touched, reports posted. A check-in freezes it. No I/O, clock or randomness | `core`, `cost`, `search` |
| `internal/jobs` | `JobSource`, the interface over `vibesys-cli` jobs | `core` |
| `internal/jobs/jobstest` | The in-memory `JobSource` and the contract every one passes | `core`, `jobs` |
| `internal/wire` | Bytes to core types and back, once, at the edge | `core`, `cost`, `gen` |
| `internal/prompts` | The words an agent is sent for a message: Go templates, goldens from the Python renderer they replaced | `core` |
| `internal/localfs` | Proof that the state directory is on a local filesystem | Nothing of this module |
| `internal/store` | The durable record: atomic commits, recovery, outbox, paged reads, retention | `core`, `wire`, `localfs` |
| `internal/store/storetest` | The contract every store passes, a workload from the real core, and a model | `core`, `store`, `wire` |
| `internal/hub` | Live deltas to the clients watching an agent: merged, bounded, never stored | `core`, `gen` |
| `internal/daemon` | The engine, effect worker, sidecar streams and `OrchestratorService`. No clock, no real I/O | `core`, `cost`, `store`, `jobs`, `wire`, `hub`, `gen` |
| `internal/daemon/daemontest` | The Fakes and the `Rig` that wire a whole daemon in memory | `daemon` and what it imports |
| `internal/client` | The client library and `connect`, over the public service API | `gen` only |
| `clientcli` | The verbs of `vibesys-client`: `run start`, `list`, `attach`, `get`, `wait`, `steer`, `update`, `stop`. Holds no run state; every real input (terminal, clock, signals, the way to the host) arrives in its `Env`. One of two packages, with `gatewayroot`, that `vibesys-client` imports | `clientcli/attachview`, `client`, `gen` |
| `clientcli/attachview` | The pure display layer of `run attach` and the run as a client sees it (`View`, `Describe`): model, inputs, outputs, `Update`, line kinds and `Render`, names, key decoding, `Sanitize`. No I/O, clock or stream | `gen` |
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
| `--repositories-directory` | where provisioned repositories go when a request names no parent: an absolute path, or `~` for the host user's home | `~/.vibesys/repos` |
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
vibesys-client run start --task T [--repository DIR] [--brief TEXT] [--harness claude_code|codex] [--model M] [--allow-agent HARNESS[:MODEL]]... [--max-time DURATION] [--max-cost DOLLARS] [--summary-every DURATION] [--detach] [--json]
vibesys-client run list                       # runs, newest first; starts that never ran are hidden, `--all` shows them
vibesys-client run attach [RUN] [--agent AGENT] [--full-history]  # follow a run; Ctrl-C detaches and never stops it
vibesys-client run get [RUN] [--json]         # status, agents, cost, checks, best, last report
vibesys-client run wait [RUN] --until started|report|progress|stalled|ended --timeout 30m [--after N]
vibesys-client run steer [RUN] "text" [--agent AGENT]
vibesys-client run update [RUN] [--max-cost DOLLARS|none] [--max-time DURATION|none] [--fanout N] [--harness H] [--model M] [--effort E] [--json]
vibesys-client run stop [RUN] [--yes]
```

`run update` changes a live run and says what changed, a line to a setting. A limit is set, changed or removed (`none`); the
time limit is the total from the run's start. A limit already passed is reached as any other: the lead is told to finish and the
run stops when it has or after the grace, and a run that reached a limit refuses a change to that limit. Lowering `--fanout` stops
no agent: no spawn or resume is admitted while the live subagents are at or over it. A lead flag asks for another tool or model:
the lead's turn ends, its session closes, and a fresh session of the same agent opens on the new spec with a handover of the run's
state (what the old lead last reported, who is live, spent and limits) as its first prompt, and the mail the old lead had not read
after it. A change of the lead takes effect when the lead rests, is replaced by a later one that is asked for before then, and is
dropped if the run stops first. An update is all or none, and each setting it changes is one entry of one `RunUpdated` event.

A run spends only on agents its operator chose. It may start subagents on its lead's harness and model, and
on each `--allow-agent` (a harness alone allows that harness's default model). A spawn that names neither
takes the lead's harness and the first allowed model on it; a spawn outside the allowance gets
`REFUSAL_AGENT_NOT_ALLOWED` with the list, and starts nothing. `Run.allowed_agents` holds the allowance and
`run get` prints it as "may use". `run get` also counts the agents that ended without a measured cost, since
their spend is missing from the usage total.

A host may also limit the models any agent runs on. Its policy is the `allowed_models` list of
`~/.config/vibesys/host.toml`, the file the Python engine reads for the same key (`tests/corpus/host_settings`
holds the cases both readers agree on), read afresh at each start. With a list, a start or spawn whose model
is not in it gets `REFUSAL_MODEL_NOT_ALLOWED`, whose receipt names the model and lists the allowed ones; a
model left out is resolved before the check, to the first allowed model the harness runs (its catalog comes
from the session holder's `catalog` argument, so from agentshim), and refused when none can be found. A file
that cannot be read allows nothing. The daemon looks once more before it opens any session, so a launch
committed under an older policy fails with `START_FAILURE_REASON_MODEL_NOT_ALLOWED` and never reaches the
sidecar. The watchers' snapshot carries the `ModelCatalog` a client offers.

A run has no limit unless `run start` is given one, and a start without one prints a line saying so and how
to set one. `--max-time 30m` is the run's time limit, counted by the host from the start (the host's clock,
not the client's: the host is usually another machine). `--max-cost 5` is its cost limit in dollars. When
the first of them is reached, `Run.limit_reached` and `limit_reached_at` record it and the core, in the one
`Decide` that saw it:

- tells the lead once, with a `RunLimitReached` message that a template words (which limit, how long the run
  has gone, what it has cost, how long the grace is), to post its final report and start nothing new;
- refuses every further spawn with `REFUSAL_TIME_LIMIT_REACHED` or `REFUSAL_COST_LIMIT_REACHED`;
- arms `TimerLimitGrace`, and stops the run `core.LimitGrace` (10 minutes) later. The run also stops as
  soon as the lead posts a final report. A lead that had already given its final report is not told: the
  run stops at once.

A run stopped this way ends as `ended`, not `failed`, with `Run.stop_reason` `TIME_LIMIT` or `COST_LIMIT`.
`run get` and `run wait` name it ("ended by cost limit"), so a wait for a report on such a run returns at once,
failed, with that reason, and never waits past the end of the run.

The cost counts what the agents' finished turns reported, and is checked when a turn ends. So a run can pass
`--max-cost` by what is in flight when the limit is reached, which the grace and the refused spawns bound but
a long turn can still make a good part of a dollar. A turn whose provider reported no cost adds nothing: Claude
Code reports its own, and Codex's is priced from its tokens by the agent library, which has no price for a
model it does not know, so `--max-cost` never takes effect for such a model. The orchestrator cannot tell that
at the start, since the library prices a turn only when it ends; `run get` says "no agent has reported a cost,
so this limit cannot take effect" once a turn has finished without one, and the time limit always holds.

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
orchestrator reminds the lead after 10 idle minutes (at most 3 times per run); a run that still stands still
10 minutes after the third reminder ends with the stop reason `stalled`. A lead that has given a final report and
has started no new work since (spawned an agent, started a job, posted a progress report) is finished, not
stalled: it is not reminded, and the run ends on its own, with the stop reason `finished`, as soon as the
lead rests and no agent, job or message is outstanding. A wait for a report on a stalled run goes on to its
timeout, which names the stall; a wait on an ended run returns at once.

The orchestrator also asks the lead for a progress report about once an hour, so that a client can show a
summary of the run with its age (the time of the latest report). `--summary-every 30m` sets the interval:
at least five minutes, an hour when none is given, and a start that gives `0` is refused, so a run that wants
fewer reports names a longer interval. The schedule is three fields of the run, folded from events like the
rest (`summary_every` and `next_summary_at` are in `Run`, the time it is counted from is kept in the store):

- work moves when a message about work reaches the run: an agent started, a handoff, a job's end, or an
  agent that stopped without a handoff. A job starting, the operator speaking, the lead's own turns and the
  orchestrator's reminders do not move it;
- the first movement since the lead last reported or was last asked sets `next_summary_at` to the interval
  after that report or request, or to that moment if it has passed, and arms `TimerSummaryDue` for it. A run
  where nothing moved is never asked, so a client shows how old its latest report is, however old;
- when that timer fires for the time the run still holds, the lead gets one `ReportDue` message (a template
  words it; the turn it starts is a `schedule` turn) and answers with `post_report`, kind `progress`. A
  request the lead has not read yet is not repeated: the movement since counts as asked;
- a report the lead posts on its own, of either kind, restarts the interval from its time and cancels the
  request that was waiting, so the timer armed for it fires for nothing. A run that is stopping, has reached
  a limit, or whose lead has given a final report (until it starts new work) is not asked. A run recorded
  before the schedule existed has no interval and is never asked.

A request is a message like those the lead gets for a finished job: it steers into a turn that is running,
or waits for the next one, and no fan-out limit or busy lead turns it away. The simulation holds every one
of these rules against its own account of the schedule over random interleavings of timers, restarts,
reports and stops.

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
live there, an omitted `RUN` means the repository's most recent run to every verb but `run steer`: `run get` shows it,
`run attach` replays it, `run wait` returns at once as it does for any `RUN` that has ended, and `run stop` says it
has already ended and exits 0. `run steer` to a run that has ended is an error, since the message would be lost;
with no run ever started there, every verb says so and names `run start`. The
repository of a new run is the root of the repository around the working directory unless `--repository`
says otherwise. Exit codes: 0 ok, 1 failed or refused, 2 usage, 3 timeout, 4 service unavailable. With no
terminal nothing prompts. `run start` follows the stream until the lead is live and exits 1, with the lead's
own detail and the start failure reason, when the run died first. `run get --json` is the service's `RunsSnapshot` filtered to one run,
with its `searches` as the snapshot carried them; a snapshot cut to fit says `truncated`, and the human
summary then omits the counts it cannot stand behind.

A run's job counts and best result come from its task's job log, which the host reads and keeps in memory;
it stores no copy, so the job log stays the one source. The host follows the log of every live run. A run
that is over is read once by each host process: the first view of the log, or the reason it could not be
read, is its last. So an ended run shows its final counts after any number of host restarts, whether it
ended before or after the restart. A view that says the log is still being read (`NO_DAEMON`) always ends,
with the counts or with the reason: `NOT_ANSWERING` when no job service answered (after the host started
one), the job service's own reasons, or `STREAM_LOST` when it accepted the connection and sent nothing
within 30 seconds. A read that failed is not tried again by that host; `host restart` tries again, and the
facts the host had from the run's live period stay with the reason. A start that never ran reads as empty
without asking.

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

A store of a schema version this build does not read is refused, with what to do: this contract changed
the stored state, and no row is ever converted. The one version before the current is read as it is,
because the change between them only added (check-ins), and the open stamps the current version on it, so
a build that reads only the older one refuses it by its number (`docs/contributing/process-boundaries.md`).
A session of another protocol found after an upgrade is ended, and
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

A snapshot of whole histories holds runs and agents, and every turn and message of those runs, not items, so the snapshot
shows an agent's turns and the handoffs and reports a late watcher missed, and not what the agent said
in them (`WatchAgent` and `ListItems` page items). A run with more turns than a timeline window holds
shows its earliest in the snapshot.

### What a run summarizes

The host owns the facts a list of runs shows, and no client derives them from messages and agents it may
not hold. `RunSummary` is the run's report, its agents and the head of each objective, worked out by the
host (`internal/daemon/summary.go`, `search.Standings`):

- **The report.** The summary report is the newest report the lead posted about the whole run. A report that
  answers Catch me up covers a window, and the core marks it: `Report.scope` is `WINDOW` for a progress report
  posted in the turn a catch-up request started (the turn already says which check-in it answers), `RUN` for
  every other one, and the lead cannot choose. A final report that answers a request is still the lead's last
  word on the run, so it is `RUN`. A window report is not the summary, does not postpone the lead's next
  scheduled report (the hour is counted from the last report about the run), and does not say the lead has
  finished; it still takes its number among the run's reports. The core names the summary report
  (`RunRecord.summary_report_id`, stored, set in the event that records the report), and the host reads the
  message by id. A run recorded before the field existed has none named, and the host names it once when it
  opens its store (`nameSummaryReports`), from its newest non-window report, so no request scans a mailbox.
  A summary is always served: when the report cannot be (the store lacks the message or fails the read, or the
  summary would pass `MaxSummaryBytes`), the summary goes without it and sets `report_unavailable`, which
  degrades that run and no other.
- **The agents.** `agents_working` counts the agents launching, in a turn, or waiting on subagents
  (`AgentStatus.Working`, the one definition), with the same split by type of agent.
- **The standing.** One entry per objective: the head's value and the best the run measured (they are two things: the head is the candidate last promoted), the unit of the standing bound and the share of it each is at. Both are read from the search state, which the one measurement projection fills; nothing is calculated again.

A snapshot carries one summary for every run it lists, whatever its detail, and a stream whose request asked for
detail `SUMMARY` also receives a `summary` frame, the whole summary of a run, each time something it says
changes (a poll's events are folded first, then one frame for each run they touched). A summary frame is not an
event of the log and has no sequence; it replaces the client's summary of the run. A reader that did not ask for
summaries is never sent the frame.

A snapshot of detail `SUMMARY` holds runs and summaries only, so its size follows the number of runs and not the
length of their mailboxes. Runs are listed live first, then the newest started; if the next run and its summary do
not fit `MaxSnapshotBytes`, it and every run after it are left out, counted in `runs_left_out`, and `truncated` is
set: the oldest finished runs go first and none disappears unsaid. A run the snapshot left out stays out for
the life of the stream: no change, search view or summary of it is sent (a new snapshot lists again), so a client
never holds a run it cannot show. The frames that follow the snapshot or a replay (the search views and summaries
of the listed runs) are sent in that listing order and stop at `MaxFollowUpBytes`; a resumed stream that cannot be
refreshed within it is answered with a snapshot instead. A client then reads a run's history with
`GetRunHistory` when it opens the run: the first page holds its agents, turns and check-ins and the first messages,
and each page names the cursor of the next (zero at the last), a page holding at most `MaxHistoryPageBytes` of
messages and at least one. The history is current to the page's `sequence`; a client keeps a record it already
holds and adds the others, and the stream's changes keep it current. The whole page is bounded by
`MaxHistoryPageBytes`: the agents, turns and check-ins take at most half of the first page and say in
`agents_left_out_count`, `turns_left_out_count` and `check_ins_left_out_count` how many they left out.
A snapshot of detail unspecified is whole
histories as before, so the headless client is unchanged; it sheds search views, then check-ins, then turns and
messages before it leaves a run out, and says `truncated`.

A page of items leaves out the text of a reasoning item or the output of a tool call larger than
`daemon.ElideOver` (4096 bytes), so that a page of an agent that thought and ran a lot stays small: the
item is in the page with that text empty, and the page's `elisions` say which items and how large the
text was. `GetItem` reads one item whole by its agent and ordinal. An item that arrives in a stream is
always whole. An item carries `started_at`, when the host first saw it, beside `at`, when it was last
updated, so a finished item took the difference; an item recorded before the host kept it has none.

### What a run has cost

The host owns every number a screen shows of a run's cost: no client sums, splits or buckets anything.
`WatchRunCost(run_id)` streams a `RunCost`, the whole view, when it opens and the whole view again after
each change, so a client keeps the latest and combines nothing. It is a stream of its own and not a part
of `WatchRuns`: cost is read for one run on one screen, so a client pays for the breakdown only while it
watches, a new breakdown is one more field of one message, and the run stream and its readers do not
change. `Run.usage` and `Run.max_cost_usd` stay on the run stream for the lists that show a run's spend.

A `RunCost` holds, for the finished turns of the run (the turns `Run.usage` counts):

- the total, with its tokens by kind, and the run's cost limit;
- one entry for each type of agent (`AgentRole`), with how many agents of the type the run has had, its
  turns, its tokens and its cost, and the same for each model (an agent's harness and model);
- the spend over time as buckets from the run's start to the instant of the view, each with its start, its
  cost and its cost by model; the host picks the bucket width from a fixed ladder (one minute to a week)
  so the series stays at 48 buckets or fewer however long the run goes on, and a turn is in the bucket its
  end falls in;
- the cost of the turns that finished in the hour before the view, which is a rate in dollars an hour for
  a run that has gone on that long.

A turn is **priced** when its provider reported a cost and **unpriced** when it reported tokens and no
cost. An unpriced turn is counted, with its tokens, in every figure and adds no dollar: a price is never
made up. A cost with unpriced turns behind it is a lower bound, and `unpriced_turns` (also on each type
and model) says how many. A cost no turn reported is absent, never zero, and so is the rate.

The ledger (`internal/cost`) is a pure fold: the same turns in any order, told again or not, give the same
view, and the total, the types, the models and the buckets each hold exactly the total's cost. A stream
reads the run's turns from the store once when it opens, a window at a time, where nothing commits (as the
snapshot's reads of turns do), and adds each turn that ends from then on, so a view costs the size of the
view and not of the run. The view is as of a time:
the host's clock while the run lives (so a stream sends a fresh view each `daemon.CostRefresh` even when
nothing happens, and the last hour does not stand still) and the run's end once it is over (so a finished
run's view never changes and is sent once).

### What the host has cost

`WatchHostCost(since?)` streams a `HostCost`: what every run the host holds has cost, live and ended, for
the one screen that shows the whole host. It is the run view's figures summed by the same code, not a
second set of rules: the host's `cost.Host` holds one `Ledger` of every counted run's turns, so the split by
type of agent and by model, the buckets, the rate of the last hour, the unpriced turns and the rule that a
cost nobody reported is absent are those of `RunCost`, and a run's line in the list is that run's own total.
The host's total is the sum of what `WatchRunCost` says each run's total is.

- **The list.** `runs` holds the most costly runs, most first, each with its id, name (the run's task, cut
  to 256 bytes at a character boundary), status, cost, tokens, turns, unpriced turns and cost limit. A run
  with no priced turn follows the priced ones, in the order of the ids. It holds at most
  `cost.MaxHostRuns` (32) lines; the runs past it are added up in `other_runs`, which is absent when every
  run is listed, so a host with any number of runs sends a view as short as the contract promises and
  `run_count` is the runs listed plus the runs summed. A run that never opened is counted like any other.
- **The window.** With no `since` the view counts everything the host holds. With one it counts the turns
  that finished at or after it, in the runs that did not end before it (a run that ended before is not
  read from the store at all); the series starts at `since`, and an instant after the host's clock is
  refused with `InvalidArgument`, so a window is always a span of the past. Without a window the series
  starts with the first run.
- **The instant.** The view is as of the host's clock while a counted run is live (so the stream sends a
  fresh one each `daemon.CostRefresh`, as a run's does), and once no run is live, of the latest end among
  the counted runs, so a host with nothing live sends its last view once and never again.
- **The stream** reads the turns of the counted runs from the store once when it opens, a window at a time,
  where nothing commits (as a run's cost stream and the snapshot do), then adds each turn that ends, each
  agent that appears and each run whose status or end moves, from the events of every run. Like a run's, it
  sends one whole view for the events of a poll. The lists of models are limited as a run's are
  (`models_truncated`).

### Catching up

A viewer presses Catch me up on a run and the host stores a **check-in**: everything that happened in the run
since that viewer's last check-in, with figures the host worked out and froze when it was made. The viewer
sees the check-in again, unchanged, in the list of the ones before it. The host does the whole of it: a client
sends the press and shows what comes back, and works out nothing. `RequestCatchUp(run_id)` is a command; its
receipt answers at once with `check_in_id`; the lead is asked later, when the facts are frozen, and the
check-in's `ask_message_id` says so.

- **Who.** The viewer is the id of an install of a client and a name to show, sent as call metadata
  (`vibesys-viewer-id` and `vibesys-viewer-label`, both or neither) and stamped by the daemon onto the
  operator caller as a label. It authenticates nothing and grants nothing: there is one operator. A catch-up
  with no viewer is refused before it is decided, with `InvalidArgument` whose `BadRequest` detail names the
  missing field (`client.BadField` reads it), so the command id is not used up; a viewer that is half given,
  too long or not text is refused the same way on any call. The Go client sends `Options.Viewer` on every call.
- **The window.** A viewer's check-ins in a run follow one another with no gap and no overlap: a window starts
  where the viewer's last check-in that did not fail ended, and the first starts with the run. It ends at the
  press, or at the end of the run when the run is over, so a window never takes in what a later run did to the
  same repository. A press the clock puts before the window's start is an empty window, never a backward one.
  A press on a run that is over is allowed. A press while the viewer's last check-in is still being worked out
  answers with that check-in: no second window or effect.
- **The facts** (`WindowFacts`, `internal/window`) are a pure function of the host's records for the half-open
  window `[since, at)`: the finished turns' spend in total and by type of agent with unpriced turns counted,
  the agents started and ended, the measurements recorded, each objective's best value at the start and at the
  end with the job and commit that made it, the plans created and changed, and the reports posted. A price
  no turn reported is absent, never zero. The core cannot hold the job log, so a press commits the check-in
  `PENDING` with a `ComputeWindowFacts` effect, in the same commit; the daemon works the facts out from the
  uncapped job log, the run's turns, agents and messages, and answers with `WindowFactsReady`, after which the
  check-in is `READY` and its facts never change. If the job log cannot be read now (not read yet, not
  answering, away), or the window holds more than the contract allows, the answer is `WindowFactsFailed` and
  the check-in is `FAILED` with the reason; it does not move where the viewer's next window starts. A restart
  finds the effect in the outbox and carries it out again, and a second answer for a settled check-in changes
  nothing. The job log of a live run is **read afresh** for the facts (`daemon.JobLog`, a stream of its own
  whose first view is the whole state), after the window ended: the view the host keeps for serving follows a
  stream and can be behind it, and a result recorded inside a window that reaches it after the facts froze
  would be in no window. A run that is over uses its one final read. A record the host keeps that cannot be
  read (the store) is tried five times and then fails the check-in, so a viewer's next press, which waits for
  the last check-in to settle, is never held for ever.
- **The ask.** A run that can be asked (live, no limit reached, its lead has not given its final report) is
  sent a `CatchUpRequest` message in the same commit that freezes the facts, so the request is worded from
  them: a template states the check-in's own figures (spend, agents, measurements, each objective's best at
  the two ends with its job and commit, plans, reports) with times of day in Seattle, and the lead recomputes
  nothing. A check-in that failed asks nobody. It goes through the ordinary delivery path, except that it
  never steers into a turn that is running: it starts a turn of its own, after the lead's current one, so the
  lead is not interrupted. The report the lead posts in that turn is linked as `report_message_id`, so the
  report is exactly the answer; any other report links to nothing. The lead has **one request at a time**: a
  viewer's `CatchUpRequest` and the schedule's `ReportDue` each cost the lead a turn, so while either is on its
  way, or being read, no second is sent. A check-in made then is ready without an ask, and a schedule that falls
  due then is dropped like one that falls due behind its own (the next movement of work arms it again).
  "Writing" is `ask_message_id` set, `report_message_id` empty and that message's `delivery` not final; a
  check-in with no ask shows its facts and the reports inside its window (`report_message_ids`). The lead's
  answer is an ordinary turn of the lead: its cost is the lead's, in the next window's spend, and there is no
  reporter agent. A check-in's own answer is dated after its window and is left out of the next window's
  reports, so no report is counted twice.
- **Serving.** Check-ins reach watchers on `WatchRuns`, not on a stream of their own: the snapshot holds every
  viewer's check-ins of each run it holds (the newest 64 of each viewer, with `truncated` set when older ones
  are left out), each whole with its frozen facts, and each change to one (made, ready or failed, report
  linked) is a `RunChange.check_in` in the order of the log. A check-in is a few kilobytes and made now and then, so the stream
  of runs carries it without a size problem, and a client that folds changes over a snapshot gets the list for
  free. A viewer's own list is the check-ins with its id.
- **Where it lives.** Check-ins are entities of the core's state and so of the event log, the checkpoint and a
  `check_ins` table: durable and replayed with the run, on the host that owns the run, which is why the
  rewind list is the same from every machine that watches it. They are kept for good, with no retention: the
  rewind list is the product, a check-in is a few kilobytes at most (its facts are bounded by the contract)
  and made when a person presses a button. The state indexes them by run and by viewer in the order a run
  shows them, so reading a run's or a viewer's list scans and sorts nothing else; only the served snapshot
  cuts to the newest 64 of a viewer.

### The `run attach` view

`run attach` reads two streams and shows them as one: `WatchRuns` for the run (agents, turns, handoffs,
jobs, limits, reports) and `WatchAgent` for the conversation of the one agent being viewed, which starts as
the lead. It prints to the normal scrollback, never an alternate screen, so a person can scroll and copy.
There is no `--json`: the lines are the output, the same lines at a terminal and in a pipe.

What a view shows:

- The viewed agent's assistant text in full, and its prompts and steers clipped and labelled (`[brief]`,
  `[steer]`). Reasoning is not shown.
- Each tool call as one line, clipped to the width: `$ first line of the command`, `read PATH`,
  `write PATH`, `edit A, B`, `search PATTERN`, or the tool's name and first line. Its output is not shown,
  except that a failed call (an error, a non-zero exit code) adds a line with its exit code and the last
  non-blank line of its output.
- A line when a turn starts, saying what started it (the brief, your steer, a worker finishing, a reminder),
  and when it ends: tool calls, cost and output tokens. A turn's input-token figure is not shown, because it
  adds up every model call in the turn and reads as the size of the context, which it is not.
- The other agents and the run's milestones as short lines, as before. An item is printed once, when it is
  complete, and an update of it is not printed again.
- A history cut falls inside a turn at most once, and the view does not guess about it: that turn says no
  count of tool calls, since it would count only the items that were read.
- Agents are named by role and number in the order they started (`lead`, `planner-2`, `worker-1`), not by
  id. `run steer --agent` takes these names or an id. The view of an agent says its id, and `run get --json`
  lists them all.

At a terminal (stdin and stdout both) there are keys and a footer line with the elapsed time, the active
agents, the cost and the keys. `a` lists the agents with numbers, role and status; a number opens that
agent's view, which clears the screen, replays its conversation the same way and follows it live, showing
only that agent's items. `Esc` or `b` goes up to the agent that started the viewed one, and on to the
lead, replaying it. Ctrl-C detaches from any view, with the same message and exit code, and the run goes
on. The terminal is put back on every way out: the run ending, a failed stream, a detach (Ctrl-C, or the
process told to end with SIGTERM or SIGHUP), and a panic in a reader. Ctrl-Z does nothing while the keys are
on, because a view stopped in key mode would leave the shell in it. With stdin or stdout not a terminal there are no keys and no footer, and the lines are written one
at a time. `--agent NAME-OR-ID` starts in that agent's view, or for a pipe follows only that agent, and
ends with the run or when that agent has ended. A run that has ended is replayed and attach exits.

Colour is restrained and follows the terminal's theme: only the 16 standard colours, dim and bold, no
backgrounds. What an agent said stays in the terminal's own style; tool calls, turn lines, milestones and
the footer are dim, with the agent's name bold on a turn line; prompts and steers are cyan; a failed tool
call and its error line are red; outcomes are coloured in the milestones (a handoff, a measurement and a
job that succeeded green; a failed job, agent or turn red; a limit reached or a stalled run yellow); a
final report is bold; the agent list has bold names and coloured statuses. There is no colour unless a
terminal is at both ends, and none when `NO_COLOR` is set to anything (no-color.org) or `TERM` is `dumb`;
there is no flag. Without it, and with no terminal, the bytes are exactly the plain text. The model never
knows about colour: a line says what it means (a kind, and a tone such as good or bad), and `RenderColor`
alone turns that into escape sequences, after cutting the line to the width, so colour cannot change what
a line says or where it is cut. Every span it starts is closed, a styled line ends with a reset, and the
reset is written again when the view leaves.

A replay holds the newest 200 items of the agent viewed and says how many came before; `--full-history`
replays all of them. The items are read as `WatchAgent` sends them (the newest page), then `ListItems`
pages back from its cursor, 75 at a time, until the replay is full.

How it is built. The display layer is its own package, `clientcli/attachview`, with one job: `Update(model,
input) (model, outputs)`, pure. Inputs are frames of the two streams, keys, resizes, ticks (the time arrives
as an input) and the caller leaving; outputs are lines to print, a screen clear, a footer, a stream to open
and the end. A line is a kind and text (`LineAssistant`, `LineTool`, `LineToolFailure`, `LineMilestone`,
`LineTurn`...): it says what the line means, and `Render` alone gives it a marker, an indent and a width, so
a colour change or a full-screen view touches `Render` and the painter. The package also holds the run's
`View` and the milestone lines (`Describe`), so there is one way a change becomes a line and `run wait` and
`run get` use the same words, and `Sanitize`, which removes terminal control sequences from every string
that came from an agent or a tool before it is part of a line, the names of agents (`planner-2`) and key
decoding. It knows nothing of terminals, streams, clocks or goroutines.

`clientcli` depends on it and never the reverse: the shell (`converse.go`) owns the streams, the key reader,
the footer's cursor control and the writes, and carries out each output. A switch ends the old `WatchAgent`
and opens a new one, and every agent frame carries the generation of the subscription it was read under, so
`Update` drops a late frame of a view that was left. The raw terminal (single keys, size, resize signals) is
`gateway/internal/terminal`, handed to the verbs as `Env.Screen`; tests give them a Fake.

The boundary is held by `internal/archcheck`, which fails the build when a file of `clientcli/attachview`
imports anything but the generated messages, the protobuf runtime they need and the values-only part of
the standard library (`viewAllowed`), imports `clientcli` or a client, a stream, `io`, `os`, `context` or
`sync`, or reads the clock.

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
RELEASE/catalogue.pb        the ReleaseCatalogue: one entry per example task, built from the CLI's card reader
RELEASE/examples/           one folder per example, at its id (model-serving/flint)
```

The examples are those of `examples/registry.toml`. `orchestrator/release/assemble` lays each into the
release and writes `catalogue.pb` from the task files, so the catalogue cannot say anything the
tasks do not; an example that lives in another repository carries only its own files, and its entry
pins the clone (URL and full commit, from the checkout's submodule). `release.ExampleSource` reads an
example back from a release, and `internal/production` serves the catalogue and the trees to the host.

`vibesys-client release verify [--installed] DIR` checks DIR against its own manifest, names every
file that is missing or cannot run, prints the version on success, and needs no host. The installer
asks it, instead of keeping a path list. `--installed` also requires what install makes, the session
holder's environment.

The manifest's session holder command is `sidecar/.venv/bin/python -m vs_sidecar`, so `uv` is not
needed at run time. Every session, lead and subagent, on either harness, loads the `vibesys` plugin from
`RELEASE/marketplace` and none of the operator's own plugins, skills or configuration. The plugin is
installed once per agent into `STATE/sessions/AGENT/plugin-home`, which a resume reuses.

Because only the `vibesys` plugin is loaded, a domain plugin such as `vibesys-kernels-cuda` or
`vibesys-cli-tools` is never installed in a managed session, whatever the task's `[agent] domain` is. The
`optimize` skill accepts a domain skill that the operator's instructions name by absolute path, so
name it in the run's `--brief`: the release carries every skill under
`RELEASE/marketplace/skills/domains/<skill>/SKILL.md`, and a `--skills-directory` checkout carries them
under `skills/domains/<skill>/SKILL.md`. The lead then points every brief at that file. Without it
the lead finds no domain skill among its own and stops, as `domain-plugins.md` says.

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
retry is the same command). `CheckRepository` asks the part of that check that needs the repository alone (an
absolute path that exists and is the root of a git work tree, then that agents can be launched) for a path an operator is still typing: it answers with the
refusal's words, and with `not_ready` when the host cannot launch agents whatever the path is, and makes no run,
event or receipt. It is for the operator, and a client that gets `Unimplemented` from an older host treats the path
as unchecked and leaves the start to the check above. A repository that passes comes back with its task names (the
folders under `.vibesys/tasks`). `ListDirectory(path, include_hidden)` lists one folder of the host for the path an
operator is typing, as `production.DirectoryLister`: the folder's own children sorted by name, each with whether it is a
directory, holds a repository's `.git` and holds a task, cut at 300 entries (`truncated`) and at the call's bound, and
never read below the folder except for those marks. `~` is the host's home, dot-prefixed names come only with
`include_hidden`, and a path that is relative, missing, a file or unreadable is refused with a code and words that name
it. `vibesys-client` resolves links the same way when it compares the working
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

Whichever tool is picked is also asked which revision of its contract it speaks (`--version --json`,
a `BuildVersions`, which starts no daemon), and one below `RequiredJobToolProtocol` (the revision that
added the task schema this release's tasks are written in; pinned to `task_schema_02_since` in
`proto/vibesys/cli/v1/conformance/protocol.json`) is refused by name: a build that predates the
question counts as too old. A desktop install puts only `vibesys-host` on a machine, so the job tool
there is whatever was installed beside it earlier, and nothing else vouches that it matches. The
answer is part of the host's readiness, the one list `Identify` returns for everything a run needs
(`parts`: session holder, skills, job tool, each ready or a typed problem, `TOO_OLD` among the
reasons), which the desktop app reads when its link is Ready and again before it sends a start, so a
mismatch is a label on the host and a refused start before any agent spends tokens, not a failure in
the middle of a run. A part a run will need is added to that list, not checked somewhere else.

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
  run that was left running would lose its tools' socket. The sidecar sockets are under
  `STATE/sessions` and the gateway socket is `ROOT/host.sock`. A socket whose path would be over 100
  bytes (a Unix socket address holds 104 on macOS) goes, by one rule (`privatesocket.Place`), to
  `/tmp/vibesys-UID/<hash of its path>.sock`, a directory only that user can reach, with nothing stored:
  every program computes the same place from the same path. That holds for the default state
  directory, so a long home is not something a user meets. A state directory the operator chose with
  `vibesys-client host configure --state-directory` stays as chosen: one whose sockets would be too long
  is an error naming that setting. None of our sockets is in the runtime directory.
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

A stable release is published, and the command below installs the newest stable one. To install a
named version instead, pass `--version X` (for example `--version 0.1.0`, or a pre-release such as
`0.2.0-rc.1`); see the list below.

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
- With no version named, the installer reads the newest stable version from `vibesys-latest`. A
  pre-release (a version with a suffix, such as `0.2.0-rc.1`) never moves that tag. Name a version
  with `--version`, which is how a pre-release is installed:
  `curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/install.sh | sh -s -- --version 0.2.0-rc.1`.
  A stable release published after it becomes what an install with no version gets.
- `VIBESYS_VERSION` (the same as `--version`, which wins), `VIBESYS_INSTALL_DIR`, `VIBESYS_HOME`, `VIBESYS_RELEASES_URL` and
  `VIBESYS_INSTALL_BUNDLE=/path/to/bundle.tar.gz` (offline install) are read; see the header of
  `install.sh`.

You also need Claude Code or Codex installed and logged in, and Docker for tasks that use it.
Agents run as your user, unconfined, with permissions bypassed. The installer ends with that warning,
and `run start` prints it to stderr on every start (so `--json` output stays clean), as it prints a
note when a run is given neither `--max-time` nor `--max-cost`.

Bundles are built by `scripts/build_release_bundle.sh VERSION OS ARCH OUTDIR` and published by
`.github/workflows/managed-release.yml` on `vibesys-v<version>` tags, with attestations; the tag
`vibesys-latest` names the newest stable version. `scripts/tests/test_install.sh` builds two real
bundles and, in a clean container as an ordinary user, installs them and runs the installed product:
a start with no harness installed fails naming the missing harness (the release found its own
sidecar), and a run with scripted agents (`sidecar/e2e`, selected by
`vibesys-client host configure --session-holder`, no model called) is started, waited on, listed, stopped and shown to
leave no process behind.

## Removing VibeSys

`install.sh` has no uninstall mode. Removal is the steps below, in this order. The paths are the
defaults; if you installed with `VIBESYS_HOME` or `VIBESYS_INSTALL_DIR`, or configured a state
directory (`vibesys-client host configure --state-directory`), use those instead.

1. Stop every run and the host. `host stop --if-no-live-runs` refuses while a run is live, and
   `host stop` without it leaves a live run's agents running until the next host starts, so stop
   the runs first:

   ```bash
   vibesys-client run list                 # then: vibesys-client run stop RUN --yes, for each live one
   vibesys-client host stop
   ```

2. Stop the job daemon. `host stop` does not: the job daemon (`vibesys-cli daemon`) belongs to your
   user, not to the host, may serve `vibesys-cli` commands you ran yourself, and only
   `vibesys-client` is on your PATH. Check it, then stop it with the copy in the release:

   ```bash
   ~/.vibesys/current/bin/vibesys-cli daemon status
   ~/.vibesys/current/bin/vibesys-cli daemon stop
   ```

   A job that is running has its own supervisor process and goes on after the daemon stops, so
   read `daemon status` first if you have submitted jobs outside a run.

   One job daemon serves a user on a machine. A second build of `vibesys-cli` refuses to start a
   daemon while another build's holds the machine, and the error names the holder. To try a second
   build beside one that is busy, start it as a sandbox daemon: set `VIBESYS_CLI_SANDBOX=1` and an
   `XDG_RUNTIME_DIR` of its own, not the holder's. Where `/tmp` is on a network file system, the job
   daemon needs `XDG_RUNTIME_DIR` set to an absolute path on local disk. Details are in
   [The daemon and the supervisors](../cli/README.md#the-daemon-and-the-supervisors).

3. Delete what the install made:

   | Path | Holds |
   | --- | --- |
   | `~/.local/bin/vibesys-client` | The link to the client. |
   | `~/.vibesys/` | Every release, the `current` link, the host's socket, lock, log and `host-config.json`, and `uv` if the installer put it there. |
   | `~/.local/state/vibesys/` | The orchestrator's state under `orchestrator/`: the record of every run, and each agent's session files and working copy. This is where most of the disk goes. It also holds the job tool's task index, so keep it if you use `vibesys-cli` on its own. |
   | `~/.cache/vibesys-cli/` (or `vibesys-cli/` under `XDG_CACHE_HOME`) | A cache of the job tool, such as the docker jobs' caches. It may remain after removal; delete it if you want the space back. |

   uv keeps its cache and the Python it fetched in its own places, outside `~/.vibesys`; `uv cache dir`
   and `uv python dir` print them if you want those gone too.

Not removed, and yours to keep or delete: in each repository where you ran a task, the
`.vibesys-state` directory (plans, measurements and scratch output) and the result branches
`vibesys/result/<task>` that hold the best commits and the worker branches `vibesys-work/<task>/*`.
Deleting VibeSys touches neither, and a result
branch you delete cannot be recovered once nothing else refers to its commits.

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
