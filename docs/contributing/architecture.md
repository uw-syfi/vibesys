# Architecture

VibeSys optimizes a program against a benchmark. Coding agents propose changes, measure them
and keep the best. This page says which processes do that, what each one owns, how they talk
and where the design stops scaling. Each component's README holds implementation
and command details. The Python module graph is in
[python-module-graph.md](python-module-graph.md).

The host gateway is `vibesys-host`; deterministic jobs belong to the CLI daemon, `vibesysd`.
Agents invoke it through the client command `vibesys-cli`.

## The processes

```text
  operator
     |
 [desktop app] --typed link--> [vibesys-host]            one per user per machine
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
| App to `vibesys-host` | The run as a snapshot, then live changes; commands from the operator. For runs of the older Python engine, a tail of their append-only log | Reads are streams, writes are single commands. Local or tunneled over ssh |
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

The orchestrator also projects deterministic search state for the app: jobs, results,
judgments, promotions and plans come from the CLI daemon's contracts. This view is
ephemeral and bounded. It is replaced as a whole, with revisions meaningful only
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
5. **A read is a stream that starts with the whole state.** Then come changes tagged with an epoch and a
   sequence number. The caller applies a full state at its end marker and keeps its resume token there. A daemon
   that cannot honor a token sends the whole state again. Nobody polls a daemon for state.
6. **A write is one bounded command.** It carries an id the callee deduplicates, so repeating it is safe, and the
   caller gives it a deadline.
7. **Identity travels in the message.** Run, task and agent are part of every call. A shell receives identity through its launch environment, with the carrier names declared once in the contract.
8. **Lifetime has one owner.** A process starts and stops the daemons it owns. A shared daemon is started only by its
   own start command, when a dial finds none or an old one, and no caller ever stops it.
9. **One Fake per service, beside the client.** A contract suite runs against the Fake and the real daemon. When the
   two live in different modules, a shared transcript corpus ties them without either importing the other.
10. **Imports and capabilities hold the pattern.** An architecture check limits which packages may import a callee's
    contract, build its client, start a process, dial a socket, or read another component's files.

How they are held: generated clients are regenerated and diffed in CI, the contract suites run both Fake and
real daemon, and import and capability checks fail a link that breaks rule 1, 8 or 10. Whether a write is
really safe to repeat, and who may start a given daemon, are design decisions that need a reviewer.

### Where the sidecar link stands

The orchestrator-to-sidecar link follows the rules on ownership (1), transport (2), reading by offset (5) and
repeatable commands (6). It departs from four: it has no version handshake (4), the orchestrator reads sidecar process metadata and output and polls for readiness (3, 5),
stopping still uses process signals rather than the typed service (8), and no contract suite runs one Fake and a real sidecar (9). These are known,
listed exceptions; the rules admit no new one.

## State and recovery

Each stateful process keeps its own state on local disk and nobody else writes to it: the orchestrator keeps a
durable record of committed events and outstanding effects, `vibesysd` keeps the job store, each sidecar keeps its
event log. A process that stops comes back by reading its own record, not by asking its peers what happened.
Agent sessions outlive the process that started them, so restarting the host does not stop an agent. A step with
no recorded result runs again, which is why every write carries an id. The Python engine's version of this,
the log as the truth and a cache beside it, is in [run-recovery.md](run-recovery.md).

## Where the limits are

- **One `vibesys-host` and one `vibesysd` per user per machine.** Everything for that user on the machine goes through
  them, and a second copy would fight over the same sockets and stores. Several users or several machines mean several
  of each, and the desktop app holds one connection per host.
- **Run summaries and detailed history have different bounds.** Run snapshots fit a byte budget and
  can omit older bundles or history. Agent items are paged separately. The core tracks multiple runs;
  their jobs still compete for the machine's resources.
- **One sidecar per agent session**, so the number of live agents is bounded by what one machine can run, and
  every agent costs a process.

## Not covered here

The older Python run engine (the `vibesys` command and its libraries) has its own module graph; see [python-module-graph.md](python-module-graph.md). The TypeScript packages of the desktop app are in
[clients-architecture.md](clients-architecture.md). Confinement of agents and jobs is in
[sandboxing.md](sandboxing.md). How a search is written is in [flows.md](flows.md).
