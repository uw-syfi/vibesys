# Architecture

VibeSys optimizes a program against a benchmark. Coding agents propose changes, measure them
and keep the best. This page says which processes do that, what each one owns, how they talk
and where the design stops scaling. Each component's README holds implementation
and command details. The Python module graph is in
[python-module-graph.md](python-module-graph.md).

The host gateway is `vibesys-host`; deterministic jobs belong to the CLI daemon, `vibesysd`.
Agents invoke it through the client command `vibesys-cli`.
People and babysitting agents run the orchestrator through `vibesys-client`, a peer of the desktop app that
speaks the same typed link.

## The processes

```text
  operator
     |
 [desktop app] --typed link--> [vibesys-host]            one per root
   (any machine)                   |  hosts
                              [orchestrator] ---typed link---> [vibesysd]   one per user per machine
                                   |                                ^
                          typed link, one per agent                 | typed link
                                   |                                |
                              [sidecar]  x N                       [CLI] <--- skills
                                   |                                          ^
                              agent program  ---- runs in its shell ----------+
```

| Process | Owns | Does not own |
|---|---|---|
| Desktop app | What the operator sees: screens, local settings, the connection to each host (local, or over ssh) | Any run state. It reads and commands through typed links |
| `vibesys-client` | A terminal client of one host: start, attach to, check, wait for, steer and stop a run | Any run state or product logic. Everything goes through the orchestrator's service |
| `vibesys-host` | The front door of one machine: it accepts clients and hands each request to the typed service that answers it | Product logic. It only composes services and serves run records |
| Orchestrator (inside `vibesys-host`) | The run: its agents, their messages, a durable record of every decision, and the work still owed to the outside world | Running jobs, and the agent processes themselves |
| Sidecar | One agent session: it starts the agent program, keeps its conversation and writes down everything it does | Decisions about the run. It carries out commands |
| `vibesysd` and the CLI | Every deterministic step: jobs (check, measure), their records, plans, promotions and the result | Judgment about what to try, and any agent |
| Skills | Judgment: what to try next, when to stop. Agents load them as a plugin for the session | Anything that must give the same answer twice. That is the CLI's job |

Two splits explain most of the shape:

- **Judgment versus determinism.** Skills and agents decide; the CLI and `vibesysd` measure, record and
  compare. A number that gets compared or promoted always comes from `vibesysd`, never from an agent's
  own account of it.
- **Decision versus effect.** The orchestrator is a pure function from (state, input) to (new state, events,
  effects), plus a thin shell that commits the events, then carries out the effects and feeds the outcomes
  back as inputs. Because the decision step reads no clock and does no I/O, the same inputs always give the
  same decisions, and tests replay a whole run in memory.

## The links

Process boundaries use typed gRPC services defined by Protocol Buffers. The process that provides the service
(the callee) owns the definition; the caller generates its client from it.

| Link | Carries | Shape |
|---|---|---|
| App to `vibesys-host` | Typed observations of runs, agents, costs and hosts; commands from the operator. For runs of the older Python engine, a tail of their append-only log | Reads declare ordered, replacement, conflated or composite delivery; writes are single commands. Local or tunneled over ssh |
| `vibesys-host` to orchestrator | The same services, in the same process | A direct call. It is still the typed service, so a client cannot tell |
| Orchestrator to sidecar | Commands in; the sidecar's ordered event log out | Commands are single calls with ids. Events are read as a stream from the last offset the orchestrator committed |
| Orchestrator to `vibesysd` | Watching jobs and results of a run | A stream that starts with the whole state, then changes |
| CLI to `vibesysd` | One command from a skill: submit a job, read a measurement, promote a result | A short call, repeated safely |
| Agent to orchestrator | Typed mail between agents (handoffs, reports, steering) | A small tool server the agent program starts, speaking the orchestrator's service |

Rules for the whole picture:

- Nothing reads another process's files or output to learn a fact. If a caller needs a fact, the owner
  serves it as a typed field. Text meant for people (logs, narration, a handoff's prose) is shown, never parsed.
- The older Python engine publishes an append-only run log as a public contract. Each record carries a checksum
  and follows the shared Protocol Buffers definition. `vibesys-host` tails it and does not interpret it.
- The desktop app sees the whole system through generated types. Its protocol bindings come from the shared service contracts; display names come from the Python catalog.

### Public observation streams

A public observation stream declares how its frames relate. Sharing gRPC does not make two streams
the same kind of state:

- An **ordered** stream carries every committed value after a sequence or offset. A snapshot or page
  establishes the starting boundary; a gap requires a reset or a new snapshot.
- A **replacement** stream carries a whole view each time. The receiver keeps one frame and restarts
  from a new whole view after reconnecting; it never combines two frames.
- A **conflated** stream carries latest state by key. Entries and removals through the sync marker are
  one snapshot, later changes stay ordered per key, and intermediate values may be skipped.
- A **composite** stream declares the rule of every response arm. One cursor does not silently make
  sideband replacement views part of an ordered log.

The [public stream registry](https://github.com/uw-syfi/vibesys/blob/main/scripts/public_stream_contracts.toml)
classifies every server stream and topic on the app-facing host services. It records the initial
boundary, ordering and cursor scope, completeness, bounds, liveness, failure carrier and compatibility
gate. The always-on public-stream check discovers those RPCs and topic arms from their Protocol Buffer
owners, rejects an unclassified or stale entry and requires every arm of a composite stream. The
registry does not copy fields, limits or timing values from the schemas.

Each feed has its own freshness and failure state. A client joins feeds only by typed identity and a
declared watermark; it never assumes separately delivered frames were atomic. Missing, stale, zero and
unavailable remain different states. Agent instructions may choose optimization strategy, but throughput,
resource use, token usage, cost, objectives, correctness and workstreams reach a client only from the
typed owner of that fact, with its scope intact. In particular, host-wide GPU use is not run-attributed
GPU use.

The orchestrator also projects deterministic search state for the app: jobs, results,
judgments and promotions come from the CLI daemon's contracts. Plans are not yet visible to the orchestrator.
An agent may also declare the performance bound of an objective (`vibesys-cli bound declare`); `vibesysd`
records each version with who declared it and when, and the view carries, per objective, the versions the
run declared and the head's percent of the standing one, computed there so the app only shows it. The view
also carries each objective's measured history (every measurement of the run, with the points that set a new
best marked) and, for a task of two or more objectives, every candidate measured on all of them with the
ones no other candidate beats marked, so a chart draws a best-so-far line and a frontier and works out
neither. This view is ephemeral and bounded. It is replaced as a whole, with revisions meaningful only
within one connection. A lost or incompatible daemon makes the view unreadable
with a typed reason; it never becomes an apparently empty successful search.
The app displays that projection without rebuilding search policy locally.

## One search, end to end

1. The operator starts a run in the desktop app. `vibesys-host` passes it to the orchestrator, which commits it.
2. The orchestrator starts a sidecar for the lead agent. The sidecar starts the agent program with the
   skills plugin and tells the orchestrator what the agent does.
3. The lead agent plans and asks for subagents. The orchestrator starts a sidecar for each, in its own worktree.
4. An agent changes code and runs the CLI to check and measure it. The CLI asks `vibesysd`, which runs the job on the
   machine and records the result. The agent's identity (run, agent) travels with the call.
5. `vibesysd` reports a finished job. The orchestrator turns that into an input and wakes the agent that is waiting.
6. The orchestrator streams every change to the app. If the host restarts, sidecars keep running and the
   orchestrator re-attaches to each by reading from its last committed offset.

A run may begin in a setup phase instead. The operator chats with a lead agent that makes the run's task
in the repository. The orchestrator reads the draft through `vibesysd` and derives, from facts, whether
the task is ready (the lead's word does not count). When every check passes, the operator completes setup:
the run switches to optimizing with a fresh lead, and the run's limits count from that point. A setup run is
listed and stopped like any run, has no subagents, and never stalls. Setup spend and time are reported
apart. A run recorded before phases is an optimizing run.

## Host composition

The host only composes typed services. Its entrypoint hands control to the composition layer;
service owners hold behavior. Import and command-shape checks enforce this boundary.
Product decisions hidden behind permitted imports still need review.

## Talking to daemons

A **daemon** here is a long-lived local process that owns some state and that another process (the
caller) reads and commands. `vibesysd` and each sidecar are daemons; `vibesys-host` is the caller of both.
Every such link follows the same rules, so a new link is a variation on a known one.

1. **The callee owns the contract.** The caller generates its client from the callee's definition and never
   writes a copy of a message.
2. **Local transport in a private directory.** gRPC over a Unix socket in a directory only the user can
   enter, on a local disk. The caller checks the directory before it dials.
3. **Discovery is a written rule.** The callee documents how to find it, a golden file pins the rule and both
   sides test against it. A caller never scans for sockets or reads process-id files.
4. **The first call is a version handshake.** The answer is compatible, caller too old or daemon too old. A
   missing or incompatible daemon is a named state with a reason, never an empty answer.
5. **A read declares its delivery contract.** An ordered state read starts with the whole state, then
   changes tagged with an epoch and sequence number. The caller applies a full state at its end marker
   and keeps its resume token there. A daemon that cannot honor a token sends the whole state again.
   Protocol 3 always starts with a reset. Replacement views restart from a whole view; keyed latest-value
   state restarts from an empty keyspace and a complete snapshot. Nobody polls a daemon for state.
6. **A write is one bounded command.** It carries an id the callee deduplicates, so repeating it is safe, and the
   caller gives it a deadline.
7. **Identity travels in the message.** Run, task and agent are part of every call. A shell receives identity through its launch environment, with the carrier names declared once in the contract.
8. **Lifetime has one owner.** A process starts and stops the daemons it owns. A shared daemon is started only by its
   own start command, when a dial finds none or an old one, and no caller ever stops it.
9. **One Fake per service, beside the client.** A contract suite runs against the Fake and the real daemon. When the
   two live in different modules, a shared transcript corpus ties them without either importing the other.
10. **Imports and capabilities hold the pattern.** An architecture check limits which packages may import a callee's
    contract, build its client, start a process, or read another component's files.

Enforcement for these rules:

1. Enforced by the `proto` check group and proto-copy check. Hand-written mirrors are unenforced because identifying them needs review.
2. Enforced for the CLI daemon by runtime-directory and Locate checks. New daemon transport choices are unenforced because they need design review.
3. Enforced for CLI discovery by golden checks and `fs-reads`. A golden for a new daemon is unenforced because no check requires one.
4. Enforced for the CLI daemon by its contract suite and conformance corpus. The first call for a new link is unenforced because it needs review.
5. Enforced for public app-facing reads by the public-stream registry check, and for daemon transcripts by
   the transcript corpus and session property checks. Polling is unenforced because a check cannot distinguish it from other loops.
6. Unenforced: idempotency and deadlines need review of the operation.
7. Enforced by identity carrier golden and carrier checks.
8. Enforced in the orchestrator by `exec-confined` for process starts. Ownership of starts and stops is unenforced because it needs design review.
9. Enforced for the CLI daemon by corpus runners. Registration of every Fake is unenforced because it needs review.
10. Enforced in the orchestrator by `cli-contract-importers`, `cli-stub`, `exec-confined` and `fs-reads`. Socket dialing is unenforced because no check restricts it.

### Where the sidecar link stands

The orchestrator-to-sidecar link follows the rules on ownership (1), transport (2), reading by offset (5) and
repeatable commands (6). It departs from four: it has no version handshake (4), the orchestrator reads sidecar process metadata and output and polls for readiness (3, 5),
stopping still uses process signals rather than the typed service (8), and no contract suite runs one Fake and a real sidecar (9). These are known,
listed exceptions; the rules admit no new one.

## State and recovery

Each stateful process owns its record: the orchestrator keeps committed events and outstanding effects on local disk, each sidecar keeps a local event log, and the `vibesysd` job store follows the repository, which may be on shared disk. A process that stops comes back by reading its own record, not by asking its peers what happened.
Agent sessions outlive the process that started them, so restarting the host does not stop an agent. A step with
no recorded result runs again, which is why every write carries an id. The Python engine's version of this,
the log as the truth and a cache beside it, is in [run-recovery.md](run-recovery.md).

## Where the limits are

- **One host per root, one orchestrator per state directory.** The root's lock and a store lock enforce these; distinct roots with distinct state directories can coexist. One `vibesysd` runs per user per machine. The desktop app holds one connection per host.
- **Run summaries and detailed history have different bounds.** Run snapshots fit a byte budget and
  can omit older bundles or history. Agent items are paged separately. The core tracks multiple runs;
  their jobs still compete for the machine's resources.
- **One sidecar per agent session**, so the number of live agents is bounded by what one machine can run, and
  every agent costs a process.

## Not covered here

The older Python run engine (the `vibesys` command and its libraries) has its own module graph; see [python-module-graph.md](python-module-graph.md). The TypeScript packages of the desktop app are in
[clients-architecture.md](clients-architecture.md). Confinement of agents and jobs is in
[sandboxing.md](sandboxing.md). What may cross between our processes, and the checks that hold it, is in
[process-boundaries.md](process-boundaries.md). How a search is written is in [flows.md](flows.md).
