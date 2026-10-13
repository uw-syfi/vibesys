# Desktop connection architecture

This document owns how the desktop app (`clients/apps/desktop-main`) reaches VibeSys on a host:
the one program it runs there, the one way it connects, how a run is identified and how its
lifecycle is reported, and how the app's screens hold state. It is written as the design. Parts
not yet built are marked in the [status table](#status); the [migration plan](#migration-plan)
says what each phase replaces.

It builds on, and does not restate:

- [`wire-protocol.md`](wire-protocol.md): framing and connection semantics of the run protocol
  (`WP-*` decisions). Every run connection described here carries that protocol unchanged.
- [`tui-architecture.md`](tui-architecture.md): detached runs and the live-instance registry.
- [`web-development.md`](web-development.md) (Desktop app): how to start the app, the page's
  MessagePort bridge, and the ssh master.
- [`clients-architecture.md`](clients-architecture.md): which TypeScript package each client
  component below lives in, and the layer and runtime rules between them.
- [`sandboxing.md`](sandboxing.md): vocabulary. Nothing here touches the run environment or agent
  confinement.

## Why

The first desktop releases found the same few mistakes again and again. Each class below names the
mechanism that allowed it; the design removes the mechanism, not the instances.

| Class | Instances seen | Mechanism | Removed by |
| --- | --- | --- | --- |
| Inferred facts | Pre-ready events stamped with run id `runs` (the socket directory's parent name), latched by the web client, freezing the run view. Records without a `run_id` until setup finishes. Recent rows saved before the id was known resume another run. A resumed run recorded under its server id. | Identity is derived late, or guessed, by whoever needs it. | [Run identity](#run-identity): allocated before detaching, present in the record and on every event from the first. |
| Failure and progress only in logs | A run that fails at startup (`docker run --gpus` on a CPU host) shows "run ended" with no reason. The first image build takes about 90 s with no progress. A run that dies before a client attaches is invisible. | Lifecycle facts exist only as log lines and as the absence of a record. | [Lifecycle phases](#lifecycle-phases): typed phases with a failure reason, in the journal and the record; ended records kept with their reason. |
| Per-action shell-built remote commands | csh and fish breakage. About 6 s per `vibesys` call over ssh. Raw argparse help from an older checkout instead of a version message. | Each action is a new shell command line and a new interpreter that imports the engine. | [The host helper](#the-host-helper): one long-lived program, one fixed bootstrap line, one `hello` that names versions. |
| UI state as scattered flags and imperative DOM | Retry on an ended run. Start enabled for a stale task. Stale async replies overwriting newer state. Recent not refreshing. Hand-rolled modal focus. | State is many booleans mutated by handlers; replies are applied whenever they arrive. | [Screen stores](#screen-stores): one discriminated union per screen, a pure reducer, request tokens. |
| Retry layers and a health check that tests the wrong thing | A registry read passes while every dial fails, so retries storm. | Two retry loops (page and supervisor), and a check that is not the failing operation. | [Connection manager](#connection-manager): one loop per host; the check is the operation that failed. |
| No guards on cost and compatibility | Light commands import the engine. A strict parse of cross-version data broke when a field was added. Two app instances write the same files. Quit does not quit. | Nothing measures import cost; versions are not negotiated; no single owner of app lifecycle. | [Guards](#guards), [versioning](#handshake-and-versioning), [app lifecycle](#app-lifecycle). |

Measured on a development machine (2026-10-10, warm caches, this checkout):

| Operation | Time |
| --- | --- |
| `python -c pass` | 27 ms |
| `import pydantic` | 71 ms |
| `import vs_project.api` | 183 ms |
| `import server.relay` | 32 ms |
| `import server.instances` | 1751 ms |
| `import vibesys.api` | 1697 ms |
| `uv run vibesys instances list --json` | 3704 ms |

`vibesys instances list` pays for two interpreters: the launcher imports `vibesys.api` at module
level (for `boot_trace`, about 1.7 s), then runs `entrypoints.instances` in a child, whose
`server.instances` imports `server.api.protocol` (for the stop request), which imports
`server.chat`, `server.events`, and through them `vibesys.api` again. `server.transport.discovery`
(the lifetime lock) costs the same 1.7 s because the `server.transport` package imports both
transports eagerly. Over ssh the login shell and round trip bring a call to about 6 s. A helper that
imports only `pydantic`, `vs_project.api`, and `vs_sim.api` starts in about 250 ms.

## Components and ownership

```text
desktop main process (apps/desktop-main)              host (local or ssh)
+-------------------------------------------+        +-------------------------------------+
| AppLifecycle: single-instance lock, quit, |        | host helper (one process per        |
|   settings store (sole writer)            |        |   channel; stateless)               |
| ConnectionManager                         |  stdio | entrypoints.host_helper             |
|   HostConnection per host                 |<------>|   -> server.host_helper (channel)   |
|     link (ssh master pool | none)         |  (ssh  |   -> server.host_api   (contracts)  |
|     control channel  -> helper            |  exec  |   -> server.registry   (records)    |
|     run channels     -> helper -> relay   |  chan- |   -> server.relay (relay)    |
|     supervisor (XState machine + shell)   |  nels) |                                     |
| screen stores (pure reducers) + React     |        | detached run servers (one per run)  |
+-------------------------------------------+        |   entrypoints.server --detach       |
                                                     +-------------------------------------+
```

| Component | Owner | Role |
| --- | --- | --- |
| Helper contracts | `server.host_api` (new, light) | Pydantic models for every helper message, the registry record, launch and stop results, and `RunPhase`. Imports `pydantic` and the standard library only. The single source of the helper's JSON Schema. |
| Registry primitives | `server.registry` (new, light; split from `server.instances` and `server.transport.discovery`) | Registry root, lifetime lock, record store, `survey`. `server.instances` keeps the stop requester and composes these. |
| Helper channel | `server.host_helper` | The channel: a pure `step` that folds lines, replies, watch observations, and time into the channel's state and the outputs the shell performs (the handshake, request ids, deadlines, feature gates, the one hand-over to the relay), and the shell. |
| Helper requests | `server.host_helper_requests` | The per-method execution over role-named interfaces (`Projects`, `Directories`, `RunLauncher`, `CheckoutProbe`, the registry), and the pure decisions it needs (`negotiate`, `run_summaries`, `launch_arguments`, path completion). |
| Helper interfaces on a host | `server.host_helper_os` | The real implementations: projects through `vs_project.api`, directories as this account lists them, launches through `vs_sim.api` with the launcher's `headless_command`, the checkout through `git`. |
| Helper entrypoint | `entrypoints.host_helper` (console script `vibesys-host-helper`) | Wires real I/O, reads stdin, writes stdout. Replaced `entrypoints.stdio_bridge` (phase 5). |
| Relay core | `server.relay` (kept) | Byte relay between stdio and a run's Unix socket, with its deadlines and typed outcomes. Used by `run.connect`. |
| Detached run server | `entrypoints.server` (changed) | Takes the lock and publishes its record (with `run_id` and phase `starting/resolving`) before importing the engine; owns its phases. |
| Generated helper types | `clients/libs/contracts/src/generated/`, read through `@vibesys/contracts/host-helper` | `host-helper.schema.json` (committed, language neutral), the TypeScript types and descriptor generated from it, and the strict parse every helper line goes through. |
| Connection manager | `@vibesys/connection` (`clients/libs/connection`: a pure `state/` machine and a `shell/`) | The only code that creates links and channels. One `HostConnection` per host; its machine is the one retry loop. |
| Screen stores | the desktop features (`clients/features/*`) and `clients/apps/web` | One store per screen; pure reducer; React renders state. |
| App lifecycle | `clients/apps/desktop-main` (`src/main/app-lifecycle.ts`) and `@vibesys/settings` | Single-instance lock, the quit sequence, the settings store. |

Placement follows [architecture.md](architecture.md): the registry and relay are generic mechanisms
under `server`, with no VibeSys policy; the helper's request handling is product wiring over them;
`entrypoints` stays the composition root.

### Tach implications

New modules and edges, all downward; no module gains an edge to `vibesys.*`:

| Module | `depends_on` |
| --- | --- |
| `server.host_api` | none (pydantic only) |
| `server.registry` | `server.host_api`, `vs_sim.api` |
| `server.host_helper` | `server.host_api`, `server.host_helper_requests`, `server.relay`, `vs_sim.api` |
| `server.host_helper_requests` | `server.host_api`, `server.registry`, `server.relay`, `vs_sim.api` |
| `server.host_helper_os` | `server.host_api`, `server.host_helper_requests`, `vs_project`, `vs_sim.api` |
| `server.instances` | `server.api`, `vs_sim.api` (loses `server.transport`; keeps only the stop requester) |
| `server.api` | adds `server.host_api` (`PROTOCOL_VERSION` is defined there) |
| `server.transport` | adds `server.registry` (discovery's lock primitives move there) |
| `entrypoints` | adds `server.host_api` and `server.registry` (phase 1), `server.host_helper` (phase 4); dropped `server.relay_report` with the bridge entrypoint (phase 5) |
| `server.relay` | adds `server.host_api` (its outcomes and `ChannelEnd` are helper contracts) |

Tach edges are module granular and `server.api` is one module, so tach alone cannot keep the helper
light. The [import-closure guard](#guards) is the check that does.

## The host helper

`python -m entrypoints.host_helper` (also the `vibesys-host-helper` console script) is the only
program the app runs on a host. Locally it is a child process over stdio; remotely it is the
command of one ssh exec channel. Nothing else differs between the two.

Properties:

- **Stateless.** Every request reads the registry, project state, or run records afresh. The helper
  owns no runs: `run.start` launches a detached run server and returns; the run outlives the helper.
  Per-channel state is bounded and limited to the negotiated version, request lifecycle, and one
  active or physically stopping watch.
- **Lives as long as its channel.** It exits when stdin reaches end of stream. A dropped app leaves
  nothing behind but the detached runs it started.
- **Started fresh from the host's checkout.** The app never caches helper code on a host. The
  host's setting names a checkout (or an installed distribution); the helper that runs is that
  checkout's, so its `hello` reports exactly what will start runs.
- **Light.** Its import closure excludes the engine (see [Guards](#guards)); `hello` replies in
  about 250 ms on a development machine, budget 300 ms.
- **Concurrent and bounded.** At most 16 request or compensating workers run on one channel.
  Further worker-spawning requests receive `channel_busy` until one physically settles. A deadline
  ends the caller-visible request, but its worker and request id remain owned until then. A watch
  has at most one observation waiting for the supervisor, so a blocked sink cannot grow the event
  mailbox. Requests on one channel are otherwise served concurrently and replies carry the request's
  id, so a slow `run.start` does not hold back `instances.list`. Every request carries a deadline;
  the helper replies `deadline_exceeded` rather than going silent.

### Bootstrap

The app starts the helper with one fixed command line per host. On ssh it is the existing shell-safe
form (`sh -c '<fixed script>' ROLE xHEX...`, every datum hex encoded, see
[web-development.md](web-development.md)); locally it is a direct `spawn` with an argument vector.
The script only resolves the interpreter and `exec`s it:

1. If the host setting is a checkout (and the fallback for any host without the console script): `uv run --no-sync --project CHECKOUT python -I -m
   entrypoints.host_helper` (about 40 ms over a bare interpreter, measured).
2. If it is an installed distribution: its `vibesys-host-helper` console script, which the wheel
   ships so the bootstrap needs no `#!` parsing. The bootstrap prefers this script and falls back to
   `python -m entrypoints.host_helper` from the checkout (step 1).
3. If the checkout predates the helper (no `src/entrypoints/host_helper.py`), the script exits with
   a reserved status (`64`) and prints the checkout's version from `pyproject.toml`. The app shows
   "this checkout predates the desktop helper (version X); update it", never argparse output.

This script is the only shell text the app sends to a host. Every action after it is a typed
message on a channel.

### Channels

The connection manager opens two kinds of channel to a host. Both start with the same `hello`.

| Channel | Count | After `hello` |
| --- | --- | --- |
| control | one per host, long lived | Request and reply messages from `HelperRequest`; push messages for watches. |
| run | one per run-protocol connection the page opens (control, subscribe, chat, per `WP-ROLES`) | One `run.connect`; on `connected` the channel becomes a byte relay to the run's socket and carries the run protocol unchanged until either side ends. |

Framing is the run protocol's: one JSON message per line, UTF-8 (`WP-GRANULARITY`, `WP-FRAME-TYPE`,
`WP-NEWLINE`), read with the existing `NewlineFramer`. After `connected`, the run channel's bytes
are the run protocol's own, so the whole `wire-protocol.md` contract holds through it, as it does
through the relay. A run channel ends with the helper's exit status and, when it is
not `0`, one typed `ChannelEnd` line on stderr (`server.host_api`, in the generated schema): `client_closed`, `server_closed`, `run_gone`, `connect_denied`,
`connect_failed`, `server_stalled`, `client_stalled`, `server_failed`, `client_failed`, with the
same statuses as the bridge.

### Stream topology: why one channel per run connection

| Criterion | A. Multiplex run streams on the control channel | B. One channel per run connection, same manager, same handshake (chosen) |
| --- | --- | --- |
| Isolation (a stuck stream must not block control) | Needs per-stream credit windows in our protocol; one slow reader blocks the shared pipe unless every stream is credited. Head-of-line blocking is the default failure. | ssh already gives every channel its own flow-control window; locally each child has its own pipe. A stalled subscribe cannot delay control. |
| Flow control and backpressure | We reimplement HTTP/2-style windows, and the relay's stall semantics (`WP-DISCONNECT`: stall the producer, bounded by the write deadline) must be redefined per stream. | Unchanged: the relay's write deadline and the server's stall-the-producer policy apply per channel, as today. |
| Failure detection | One channel death ends every stream; per-stream ends need in-band close frames and their own status codes. | Each channel ends with a typed status (`run_gone` vs `link` vs `connect_denied`) already defined and tested by the relay runner. |
| Simplicity | New mux layer in Python and TypeScript, a new conformance surface, and a changed `WP-ROLES`. | Reuses the relay core and its conformance runner; `WP-ROLES` holds; the only new code is the shared `hello` prefix. |
| Cost | One helper process per host. | One helper process per run connection (about 250 ms to `connected`, measured on the interpreter and light imports). |
| Limits | None from ssh. | OpenSSH `MaxSessions` (default 10) bounds channels per master: 1 control plus up to 3 per attached run. The manager counts channels and opens a second master for the host past the limit; this is its job, not a caller's. |

B wins on every criterion that removes a bug class (isolation, failure typing, simplicity), and its
cost is a startup paid on attach, not per action. Both kinds of channel are opened through the
role-aware `Link` contract. Its closed roles are `host-control`, `run-control`, `subscribe`, and
`chat`; `run-control` maps to the helper's existing `control` wire role. The link performs admission
before the bootstrap and `hello`, and returns a typed capacity failure without changing the host
connection's health; there is no other way to reach a host.

An SSH link reserves two of its conservative eight application slots per master for control roles.
Passive subscriptions and chats spill to another owned master once they reach the non-reserved
limit. The pure admission policy owns queued demand as well as leases: it selects role priority
first and FIFO within a role, reserves a spill master before spawning, and offers a newly ready
master to the whole queue before retiring it. A helper stays leased to its chosen master until its
process exits. A pre-handshake OpenSSH session refusal is normalized by the SSH implementation and
retried once away from that master. The pool is bounded to four masters; exhaustion is a typed
channel-capacity result, not a host-link failure. Local links carry the same roles and lifecycle
contract without an artificial session limit.

The Electron message-port hop preserves that per-channel backpressure with symmetric byte credits.
The main-process relay and sandbox preload each grant one bounded window. A sender spends the UTF-8
size of the protocol frame plus its byte-stream newline, and the receiver returns those bytes only
after the page callback or host-stream write has accepted them. Exhausted page credit allows one
bounded window of read-ahead, then pauses the owned host stream; exhausted host credit keeps page
frames in one bounded preload queue. Invalid or excess credit, an oversized frame, or a queue that
would exceed the window closes the connection instead of growing memory. A complete host frame that
cannot cross the Electron hop starts a local no-progress deadline, reset by positive page credit.
This both bounds a frozen renderer and covers a clean EOF buffered by Node after credit was
exhausted; on expiry the relay ends the connection unconditionally.

### Methods

All messages are Pydantic models in `server.host_api` with `extra="forbid"`, discriminated by
`type`. Every request has `request_id` and `deadline_ms`; every reply echoes `request_id` and is
either the method's result, whose `type` is the request's, or `HelperError` (`type: error`, a closed
`code`, and the `path` for project and path errors). A line that is not a request is answered
`invalid_request`, with its `request_id` when it has one.

| Method | Request | Result | Notes |
| --- | --- | --- | --- |
| `hello` | `client_versions: [min, max]`, `features: [Feature]`, `app_version` | `protocol_version`, `features`, `vibesys_version`, `checkout: {root, commit, dirty} \| null` | First message on every channel; anything else first is `handshake_required`. |
| `instances.list` | none | `InstanceList`: `live`, `ended` (tombstones with phase and reason), `unreadable` (id and record version) | Replaces `vibesys instances list --json`. A client that negotiates no `instances_watch` takes exactly one snapshot while installing each control channel; it never polls. |
| `instances.watch` | none | `watch_id`, then `instances.changed {watch_id, listing}` pushes: the listing at once, then on each change | Removes manual refresh (Recent and host panels). Gated by feature `instances_watch`. The helper reads the registry once a second per watch, without writing to it; the client never polls. |
| `instances.unwatch` | `watch_id` | `watch_id` | Ends a watch; no push for it follows. `unknown_watch` when none is open. |
| `tasks.list` | `project` | `TaskList` | Replaces `vibesys tasks --json`. Errors are typed: `project_missing`, `project_uninitialized`, `task_invalid`, each with the path. |
| `runs.list` | `project` | the project's runs (`run_id`, `task`, `display_name`, `created_at`) with the `phase` of the server on this host that drives or last drove each, and its `instance_id` while live | Gives Recent rows status and lets resume name a concrete run. A run whose server left no record here has no phase; reading the run's own journal for it needs a light journal reader and is not built. |
| `run.start` | `project`, `task`, `args: [str]`, `resume: {run_id} \| {latest: true} \| null` | `{instance_id, run_id, socket_path, phase}` or `DetachedLaunchFailure` | Allocates or resolves `run_id` before detaching (see [Run identity](#run-identity)); returns once the record is published, not when the run is ready. |
| `run.stop` | `instance_id`, `force` | `InstanceStopResult` | Same outcomes as `vibesys instances stop`. |
| `run.connect` | `instance_id`, `role: control \| subscribe \| chat` | `{run_id, protocol_version}` then relay | Run channels only; `channel_busy` while another request or watch is open. The run's `run_id` comes back here, so the page never derives it. Bytes the client sends before the reply are relayed after it. A failed connect (`run_gone`, `connect_denied`, `connect_failed`) leaves the channel a request channel. |
| `ping` | none | an empty reply | Control channel liveness for the supervisor. |
| `paths.complete` | `prefix` | `directory`, `entries: [{name, path, vibesys_checkout, vibesys_project, git_repository}]`, `truncated` | For the host's project and checkout pickers. `~` expands on the host and a relative prefix is under the home directory; a trailing `/` lists the directory. Directories only (a link counts when this account can follow it to one), filtered by the typed name prefix exactly, sorted, at most 200. A directory is read for at most 10,000 entries; past either bound `truncated` is set. `path_missing`, `path_not_directory`, `permission_denied` name the path. Gated by feature `paths_complete`. |

There is no `projects.recent` method: Recent is the app's own record of what this user opened,
across hosts, and has one writer (the settings store). What the host knows, the runs of a project
and their status, is `runs.list`. See [Decisions and alternatives](#decisions-and-alternatives).

`run.stop` needs the run protocol's `command.stop` and `Response`, and `Response`'s payload types
import the engine. The run protocol's control envelope (the requests and responses `run.stop`
needs) is therefore split from the engine-importing modules first, as step one of phase 4 (see
[Phases](#phases)), so the helper imports only the envelope and the import guard has no exceptions.

### Handshake and versioning

`hello` negotiates two things:

- **Protocol version**: an integer. The app sends the range it speaks; the helper picks the highest
  version both speak, or replies `incompatible` naming both ranges, the helper's `vibesys_version`,
  and the checkout root. The window shows that message and the action that fixes it (update the
  checkout, or the app).
- **Features**: a closed `StrEnum` per protocol version. The reply is the intersection.

The rules:

1. **Strict within a version.** Both ends parse every message with `extra="forbid"` against the
   negotiated version's schema. An unknown field is a bug and fails loudly, naming the field.
2. **Additive evolution through features.** A new optional field or method ships behind a new
   feature. A sender includes the field or calls the method only if `hello` returned the feature,
   so a strict peer never sees what it does not know. This replaces probe-by-rejection
   (`WP-CAPABILITY-PROBE`) for the helper protocol; the run protocol keeps its probe until it gains
   a `hello` of its own.
3. **Breaking changes bump the version.** Removing or retyping a field, or changing a method's
   meaning, is a new protocol version. A helper supports the previous version for one release.
4. **Durable data carries its own version.** Registry records, launch failures, and stop results
   have a `version` field. A reader parses every version it knows strictly, through explicit
   upgrade functions. A record of a version it does not know is reported as
   `unreadable {id, version}`, never dropped and never a failure of the whole listing. Two
   checkouts on one host share one registry, so this case is normal, not exceptional.

The helper's `protocol_version` is independent of the run protocol's `PROTOCOL_VERSION`; the run
protocol version is reported per run in `connected` and in the record.

## Run identity

The detaching parent allocates the run id before the run server exists:

- **Fresh run**: `run.start` (and `vibesys --detach`) calls `vs_project`'s `generate_run_id` and
  passes it to the server child (`--run-id`, which sets the existing `RunRequest.run_id`).
- **Resume**: `resume: {latest: true}` is resolved to a concrete run id from the project's durable
  state before the child starts; the child receives `--resume RUN_ID`. "Latest" never reaches the
  server, so the record names the run that was actually resumed.

The id is then present everywhere from the first byte:

- `LiveInstanceRecord.run_id` becomes required (record version 2). The server takes its lock and
  publishes the record before importing the engine, so `instances.list` shows the run, with its
  id, within the helper's startup time.
- Every journal event carries the run id from the first one. The value previously derived from the
  socket directory is gone; a pre-ready event without a run id is not representable.
- `run.connect`'s `connected` carries the run id, and the subscribe ack (`WP-SUBSCRIBE-ACK`) names
  the same run. Clients latch identity from the attach, never from data.
- The app stores Recent rows by `(host, run_id)` at the moment `run.start` replies, never later.

The instance id remains the identity of one server process (one lock, one socket); a run resumed
twice has one run id and two instance ids over time. Nothing in the app keys run state by instance
id.

## Lifecycle phases

One closed union, owned by `server.host_api` and folded by the existing pure lifecycle
(`server.run_lifecycle`), is the only lifecycle vocabulary the app renders:

| `RunPhase` | Meaning | Ends |
| --- | --- | --- |
| `starting {stage, progress}` | `stage` is `resolving`, `building_image`, or `starting_container`. `progress` is `{done, total \| null, label}` when the stage reports it. | no |
| `running` | Ready and executing (`RunStatus.RUNNING`; also `pausing`, `paused` as today). | no |
| `stopping` | A stop was accepted; the run ends at its next boundary. | no |
| `stopped` | Stopped by request, state persisted, resumable. | yes |
| `failed {reason}` | `reason` is a `Diagnostic` (code, stage, message, log path). | yes |
| `finished` | Completed (`RunStatus.COMPLETED`). | yes |

This extends `RunStatus` rather than adding a parallel enum: `starting` gains the startup stage and
progress, `failed` gains its reason, and the record's `phase` is a projection of the same fold that
emits `run_status_changed`, so the two cannot disagree.

Facts enter once:

- **Journal**: a new event `run_startup_progress {stage, progress}` and the existing
  `run_status_changed` (now carrying the reason on `failed`). The agent image build
  (`vs_agent.api.images.agent_image`) parses Docker's progress output into typed step
  observations (`ImageBuildProgress {done, total, label}`), not log lines; the run environment
  reports them, and the stage it is in, to the request's `startup` sink (`vs_runtime`), and the
  engine emits each as a `run_startup_progress` core event. The run's status stays `starting`
  until its session reports it ready (`RunTrigger.READY`); attaching the journal is not readiness.
  A pause or stop requested while starting is held (`RunState.requested`), not entered: the run
  keeps reporting `starting` with its stage and progress, a resume drops the held request, and
  `READY` lands the run in `pausing` or `stopping` if one is still held. Core may land a held stop
  before `READY`, abandoning the startup; the run then goes from `starting` straight to `stopped`. A failure while starting past `resolving` names the startup stage it failed in.
- **Record**: the server rewrites its record atomically (temp file, rename) on each phase change;
  a change of progress alone is written at most once per second, the latest one when the second
  ends.
- **Ended records**: when a server exits, its last record (terminal phase and reason) stays in the
  registry as a tombstone for 24 hours; the next listing after that removes it, with no coupling to
  client acknowledgements. Finished runs stay reachable from run history (`runs.list`), not the
  live registry. `survey` reports a free lock with a terminal record as
  `ended`, and a free lock with a non-terminal record as `ended {failed: crashed}` with the server
  log path. A run that dies before any client attaches is therefore visible with its reason, and
  the bounded wait for a first client (the current fix for that symptom) is no longer needed.
- **Who writes**: only a writing listing (`LiveRegistry.list`, behind `instances list`,
  `instances.list` and `runs.list`) buries crashed servers and removes expired records. A watch
  reads with `LiveRegistry.read`, which writes nothing: it reports a crashed server not yet buried
  as `failed {crashed}` with `ended_at: null`, so a once-a-second read reports the same listing
  until something changes, and it leaves expired records on disk, unreported. Watches poll rather
  than wait on file events because a crashed holder releases its lock without touching any file.
- **Accepted gaps across record versions and crashes**:
  - A tombstone of a newer record version is never aged by an older reader: it is reported
    `unreadable` and left for that version's lister to remove, so a host that only ever runs the
    older checkout keeps it.
  - A crashed run's `ended_at` is when a writing listing found it dead, not when it died: the
    server wrote nothing as it died, and the lock carries no time. Its 24 hours run from then.

The UI renders only the phase: Retry appears only on a link failure, never on an ended phase;
Resume appears only on `stopped`, `failed`, and `finished`.

## Connection manager

One `ConnectionManager` in the main process owns every link and channel. Windows and screens ask it
for a channel; they never create one.

```text
HostConnection states (an XState machine, @vibesys/connection state/host-connection.ts)

  idle --open--> linking --link up--> handshaking --hello ok--> ready
                   |                      |                       |
                   |auth needed           |incompatible           |channel end (link) or ping timeout
                   v                      v                       v
               auth-needed          incompatible            reconnecting(attempt) --check ok--> ready
                                                                  |
                                                                  |backoff spent
                                                                  v
                                                               offline --wake / network / user--> reconnecting
  any --close--> closed
```

Rules:

1. **One loop per host.** The `HostConnection`'s supervisor is the only retry loop. On desktop the
   page's `WebSession` keeps its resume logic (cursor, `store_id`, reconcile) but has no reconnect
   timer of its own: its dial asks the manager for a run channel and waits until the host is
   `ready` or the manager reports a terminal state. The current page-side backoff is replaced on
   desktop by "wait for ready".
2. **The check is the failing operation.** A run channel that ended with a link class status is
   checked by opening a run channel to the same run (`hello`, `run.connect`) and getting
   `connected`. The control channel is checked by `ping`. A registry read never stands in for a
   dial.
3. **Typed causes decide.** `auth-needed`, `incompatible`, and `run_gone` stop the loop; `link`,
   `server_stalled`, `client_stalled`, and connect timeouts retry with the finite backoff; a
   `connect_denied` stops at once. The causes are the closed set in `server.host_api`, generated
   into TypeScript.
4. **Warm links.** At launch the manager opens ssh masters for the hosts in Recent (no prompts; a
   host that needs credentials stays `auth-needed` until the user signs in). Target: a populated
   host panel within 1 s of choosing a host with a warm master (one channel open, `hello`,
   `instances.list`).
5. **Closing the app closes channels and masters, never runs.**
6. **Work belongs to its state.** The machine is XState (`clients-architecture.md`, "State"). Each
   operation (link, handshake, ping, check) is an invoked actor the shell provides, and each wait
   (the operation's deadline, the heartbeat, a backoff slot) a delayed transition on the clock the
   shell injects; leaving a state stops its operation and cancels its delays, so there are no
   operation or timer ids to compare. A control channel an operation opened is installed only by the
   machine accepting that outcome (`installControl`), after the shell's `current` guard confirms the
   channel still serves; an outcome it does not accept hands the channel back (`discard`), and one
   it stopped waiting for is closed when the operation's signal aborts. A check that finds the host
   answering but the run gone emits `run-unreachable` and returns to `ready`.
7. **SSH admission is role-aware and link-owned.** A pure capacity policy owns queued demand and
   leases, reserves operator slots, selects demand by role priority with a FIFO tie-break, selects
   the least-loaded eligible master deterministically, and authorizes at most one new master for
   concurrent demand. The SSH shell interprets its start/stop/grant results, releases leases on
   every exit path, and exposes only bounded per-role counts. Connection and UI code never see
   control paths, commands, credentials, or host configuration through those diagnostics.

## Screen stores

Every screen (welcome, host panel, start-run sheet, run strip) has one store:

- **One discriminated union per screen.** For example the start-run sheet is
  `{kind: 'editing', project, tasks: {for: project, list} | null, picked} | {kind: 'starting',
  token, ...} | {kind: 'failed', failure}`. Facts that must agree live in one variant, so "Start
  enabled for a task listed from another project" is not representable.
- **Pure reducer.** `step(state, action) -> {state, requests}`. No I/O, clock, or DOM in the
  reducer. (A store whose state is control state, with work to start, wait for, and cancel, is an
  XState machine instead, as the supervisor is: `clients-architecture.md`, "State".)
- **Request tokens.** Every request the reducer emits carries a token stored in the state that
  awaits it. A reply whose token is not the awaited one is dropped by the reducer. A stale task
  list or overview reply cannot overwrite newer state, by construction.
- **Render is a function of state.** React components read the store and dispatch actions; they hold
  no state that changes behavior. The welcome view moves from imperative DOM to React (already used
  by `clients/apps/web`).
- **Native `<dialog>`.** Sheets are `<dialog>` opened with `showModal()`: focus containment, inert
  background, Esc, and return of focus to the opener come from the platform.
- **Data from watches.** Host panels and Recent statuses fold `instances.watch` pushes; there is no
  manual refresh path to forget.

## App lifecycle

`AppLifecycle` is the one owner of process-wide state:

- **Single instance.** `app.requestSingleInstanceLock()`; a second launch focuses the first and
  forwards its arguments.
- **Quit sequence.** A pure machine `running -> quitting(releasing) -> exiting`: the first quit
  request starts a bounded release (close channels and masters), the second request or the bound
  exits. There is exactly one `app.exit` call site.
- **Settings store.** `hosts.json` and `recent.json` have one writer, the store, which serializes
  updates and replaces files atomically. With the single-instance lock, no other process writes
  them.

## Guards

| Guard | Tier | Asserts |
| --- | --- | --- |
| Import closure (`tests/architecture/test_light_entrypoints.py`, new) | deterministic | The static module-level import closure (AST, excluding `TYPE_CHECKING` blocks and function-local imports) of `entrypoints.host_helper`, `entrypoints.instances`, `entrypoints.tasks`, and the launcher's dispatch stays within an allowlist: the standard library, `pydantic`, `vs_sim.api`, `vs_project.api`, `server.host_api`, `server.registry`, `server.host_helper`, `server.relay`, and the run protocol's control envelope. The allowlist has zero exceptions: no module is listed as a temporary hole. No `vibesys.*`, `vs_runtime`, `vs_agent`, `mcp`, `server.api`, `server.events`. |
| Helper startup | real (`tests/e2e`) | A real helper answers `hello` and its loaded-module set (reported by `python -X importtime`) is within the allowlist and under a module-count ceiling that only shrinks. Wall time is reported as a metric against the 300 ms budget, not asserted, because a test's result must not depend on timing. |
| Helper conformance | deterministic and real | Scenarios under `tests/conformance/scenarios/` (`helper-*`) are request and reply transcripts. Runner `tests/conformance/runners/host-helper.json` replays them against the Python helper in process (deterministic) and against any helper command over stdio (real), so another implementation is tested by pointing the runner at its command. |
| Store invariants | deterministic | For each screen store, seeded random sequences of actions and replies (including stale, duplicated, and reordered replies) are folded through the reducer; after every step the store's `invariants(state)` hold (for example, no Retry on an ended phase), every awaited token was issued and no two request slots await one token, no reply is applied with a stale token, and per-store behavior properties pass (a start action asks to start a run only when Start is enabled). The generator runs across many seeds with no new dependency, and failing seeds are printed for replay, then pinned as examples. |
| Supervisor and quit machines | deterministic | Same model-based style over `HostConnection.step` and the quit machine: no orphan waits (every waiting state has a timer or channel that will produce its event), terminal states are absorbing. |
| Journeys | real (CI job) | Playwright drives the real Electron app against a host that runs the real helper locally: connect, start (with a fake run that reports startup stages), attach, drop the channel, stop, resume, quit. Assertions are on rendered phase and identity, with bounded waits that only guard hangs. |
| Record versions | deterministic | Property: a listing over any mix of known, unknown, and malformed record versions returns every id exactly once, in `live`, `ended`, or `unreadable`. |

## Contract pipeline and IDL

Helper messages join the existing pipeline: Pydantic models in `server.host_api` produce
`clients/libs/contracts/src/generated/host-helper.schema.json` (committed) through
`server.api.schema`'s generator, and TypeScript types are generated from that schema, as for the
run protocol (see the generated directory's README). CI fails when the committed schema is stale.
The committed JSON Schema is the language-neutral contract; the conformance corpus is the
executable one.

### IDL: JSON Schema versus protobuf

Requirement: keep the freedom to implement a backend (helper or run server) in another language.
Scores are 1 (poor) to 3 (good).

| Criterion | JSON Schema from Pydantic (chosen) | Protobuf |
| --- | --- | --- |
| One source of truth with today's protocol | 3: same pipeline as the run protocol; no second IDL | 1: the run protocol is already JSON Schema; protobuf means two IDLs or migrating everything |
| Expressiveness for our contracts | 3: discriminated unions, literals, closed enums, constraints, `extra="forbid"` | 2: `oneof` and enums exist; open enums and lack of required fields weaken "closed set" |
| Strictness | 3: unknown fields rejected, matching our rule | 1: unknown fields are preserved by design; strictness must be added by hand |
| Codegen in other languages | 2: mature generators (quicktype, typify, go-jsonschema, datamodel-code-generator), less uniform | 3: first-class generators everywhere |
| Evolution rules | 2: by our convention (feature flags, version bump) | 3: field numbers make additive evolution mechanical |
| Debuggability over ssh and in logs | 3: JSON lines readable in a terminal and in the corpus | 1: binary; needs tooling to read |
| Framing fit | 3: JSONL already, shared with the run protocol | 2: needs length-prefixed framing, a new `WP` decision |
| Payload size and speed | 2: fine for control and event traffic | 3: smaller and faster |
| Total | 21 | 16 |

Recommendation: JSON Schema. Protobuf's real advantage, mechanical evolution, is recovered by the
`hello` feature rule; its costs (a second IDL, lost strictness, binary debugging) hit exactly the
classes this design removes. Revisit only if event volume makes JSON a measured bottleneck.

A non-Python backend:

1. Generates its types from the committed `protocol.schema.json` and `host-helper.schema.json`
   (for example `typify` for Rust, `quicktype` for Go or Swift), with unknown fields rejected.
2. Implements the helper methods and the run protocol's transport decisions.
3. Proves conformance by running the corpus runners against its command: the helper runner over
   stdio, the server scenario runner over its Unix socket, and the event fixtures through its
   serializer. The corpus gate already fails when a schema kind has no fixture.

### Helper versus a light per-call CLI

| Criterion | Light per-call CLI (`vibesys instances list` made cheap) | Long-lived helper (chosen) |
| --- | --- | --- |
| Latency per action | interpreter plus light imports per call (about 250 ms) plus a channel open | one round trip on an open channel (milliseconds) |
| Shell exposure | a command line per action, quoted for every login shell | one fixed bootstrap line per channel |
| Typed errors | exit codes and stderr text per command | `HelperError` with a closed code set |
| Version negotiation | none per call; an old checkout prints argparse usage | `hello` once, with a typed `incompatible` |
| Push updates (watch) | not possible; clients poll | `instances.watch` |
| Run connections | separate entrypoint (`stdio_bridge`) | same program, same handshake |
| Statefulness risk | none | none in practice: the helper is stateless across requests |

The light CLI is still worth having for people (phase 1 makes it light), but the app does not use
it.

## Migration plan

Deleted when the corresponding phase lands:

- `entrypoints.stdio_bridge` and the bridge's own command line (phase 5, once the app connects
  through `run.connect`; the relay core stays).
- `Host.invoke` and every per-action ssh script: `instances list`, `tasks`, `--detach` start and
  resume, `instances stop` (phase 5).
- `host.ts`, `detached-host.ts`, `ssh-host.ts`, `local-host.ts`, the bridge protocol (now
  `@vibesys/ipc`'s `bridge.ts`), and the per-command parts of `shell-words.ts` (now in
  `@vibesys/feature-start-run`) and `instances.ts`, replaced by the connection manager
  (phase 5). The shell-safe encoding survives only for the bootstrap line.
- The registry check in `supervisor.ts` (replaced by the real-operation check) and the page's
  desktop reconnect timer (phase 5).
- Run id inference: the empty-id convention for pre-ready events and the client's latch-on-first-id
  (phase 2).
- The bounded wait for a first client after an early failure (phase 3, superseded by tombstones).

### Phases

Each phase merges on its own and leaves the app working.

| Phase | Scope | Acceptance |
| --- | --- | --- |
| 1. Light host commands | Split `server.registry` (lock, store, survey) and `server.host_api` (records) out of `server.instances` and `server.transport.discovery`; stop `server.transport`'s package `__init__` from importing both transports; make the launcher's `boot_trace` mark light and dispatch `instances` and `tasks` in process. Add the import-closure guard. | Guard test green. `vibesys instances list --json` and `vibesys tasks --json` import none of the denied modules; measured time reported in the PR (target under 400 ms locally). No behavior change. |
| 2. Explicit run identity | `--run-id`; resume target resolved before detaching; record version 2 with required `run_id`, published before engine import; every event carries the run id; listing reports unknown record versions as `unreadable`. App stores Recent by run id at start. | Regression tests for the inferred-identity class (first event of a detached run names the run; resume latest records the resolved id; listing over mixed versions). Web client no longer latches identity from data. |
| 3. Lifecycle phases | `RunPhase`; `run_startup_progress`; image-build progress from `vs_agent`; record phase updates; tombstones with reason. App renders phase only. | A run failing at startup shows `failed` with its reason in `instances.list` with no client attached. Image build shows progress. Property test for listing tombstones. |
| 4. Host helper | `server.host_api` messages in the schema pipeline; `server.host_helper` and `entrypoints.host_helper` with every method; helper conformance scenarios and runner; delete `entrypoints.stdio_bridge`. Step one (a small phase 3.5 if it lands separately): split the run protocol's control envelope (requests and responses needed by `run.stop`) from the engine-importing modules. | Step one: the import-closure guard passes with zero allowlisted modules. Then: conformance runner green in process and over a real process; startup guard green; the app still works on today's code paths (the helper is additive until phase 5). |
| 5. Connection manager | `ConnectionManager`, `HostConnection` with pure step; bootstrap line; channels; real-operation checks; warm masters; local uses the same path. Delete the per-action scripts and host implementations listed above. | Contract suite over a fake helper and over the real helper; journey test (connect, start, attach, drop, stop, resume, quit); connect to a populated host under 1 s with a warm master (measured, reported). |
| 6. Screen stores and app lifecycle | React welcome, host panel, start-run sheet, run strip on per-screen stores with tokens and `<dialog>`; `AppLifecycle` with single-instance lock, quit machine, settings store. | Model-based invariant tests per store; quit and second-launch journey steps. Can land before phase 5 against today's welcome bridge. |

Phases 2, 3, and 6 do not depend on 4 or 5 and may proceed in parallel after phase 1.

## Status

| Part | Status |
| --- | --- |
| Run protocol, framing, conformance corpus | built |
| Detached runs, live registry, stop and resume | built |
| Stdio relay core | built (run channels; the bridge entrypoint is deleted) |
| Pure supervisor core | replaced by the connection manager's machine (phase 5), an XState machine since phase 8 of #1878 |
| Light host commands and import guard (phase 1) | built |
| Explicit run identity (phase 2) | built (server side; the desktop app still keeps its id inference until phase 5 or 6 removes it) |
| Lifecycle phases, progress, tombstones (phase 3) | built: phases with failure reason in the record, tombstones, startup stages and image-build progress (`run_startup_progress`, record version 4); the desktop renders phase, stage, and progress in the live run table, and ended-run tombstones with their reason in the host panel |
| Control envelope split (phase 4, step one) | built (`server.control_envelope`) |
| Host helper and its contract (phase 4) | built: every method, `host-helper.schema.json` and its TypeScript types, the `helper-*` conformance scenarios and runner (in process and over stdio), the import-closure and start-up guards; `entrypoints.stdio_bridge` deleted in phase 5 |
| Connection manager (phase 5) | built: `ConnectionManager` and `HostConnection` (an XState machine, shell with disposal scopes), the bootstrap, typed channels with one strict validator, role-aware SSH admission with spillover masters and bounded diagnostics, run channels, watch-pushed listings, path completion, the HostConnection contract suite over the `FakeHelper` and the real helper; not built: the Playwright journey job and checkout completion on a host with no checkout yet (it needs a helper) |
| Screen stores and app lifecycle (phase 6) | built (#1839) |

## Decisions and alternatives

Maintainer decisions on the questions this design first left open.

1. **Host-side `runs.list` replaces `projects.recent`; Recent stays app-owned.** Alternative: a
   per-user recent-projects list kept on each host. Reason: one writer for the Recent list; the
   host only reports facts it owns.
2. **Ended-run tombstones are kept 24 hours, then removed by the next listing.** Alternative: keep
   until a client acknowledges the ended run. Reason: no coupling to client acknowledgements; a day
   covers reattach after sleep, and a weekend is out of scope because finished runs stay reachable
   from run history, not the live registry.
3. **No new `fast-check` dependency.** Store invariant tests use a seeded generator run across many
   seeds, with failing seeds printed for replay. Reason: avoids a dependency for what the existing
   seeded approach already gives; revisit if shrinking becomes necessary.
4. **The wheel ships a `vibesys-host-helper` console script.** The bootstrap prefers it and falls
   back to `python -m entrypoints.host_helper` from the checkout. Reason: installed hosts need no
   `#!` parsing.
5. **Split the run protocol's control envelope before the helper lands.** Alternative: accept one
   lazily imported stop path as a guard exception in phase 4. Reason: an exception in the guard is
   exactly the kind of hole the guard exists to prevent.
