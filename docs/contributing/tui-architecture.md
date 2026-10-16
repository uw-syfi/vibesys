# TUI architecture

The Python packages follow one inward dependency direction:

```text
entrypoints -> server   -> vibesys.api
entrypoints -> headless -> vibesys.api
```

`vibesys` is the optimization library. It owns run execution and the durable
core event journal, and does not import serving or process-entrypoint code.
`server` and `headless` are peers over the `vibesys.api` facade; neither imports
the other. `server` projects core events into its wire journal and owns
frontend-specific facilities such as experiment chat. `headless` runs and renders a
run without the server. `entrypoints` composes either the headless integration
or the server runtime. Tach enforces this direction in CI; see
[architecture.md](architecture.md) for the generated graph.

The TypeScript frontend packages have one allowed dependency direction (the full graph is in
[clients-architecture.md](clients-architecture.md)):

```text
@vibesys/contracts <- @vibesys/transport <- @vibesys/transport-node <- @vibesys/tui
@vibesys/contracts <- @vibesys/transport <- @vibesys/app-web
@vibesys/contracts <- @vibesys/run-state <- @vibesys/tui, @vibesys/app-web
```

`@vibesys/tui` and `@vibesys/app-web` may depend on the lower layers. They never depend on each other,
and reverse imports and cross-package relative imports are forbidden and checked by
`pnpm check:ts-architecture`.

Dependency-cruiser parses and resolves the TypeScript graph for dependency direction, cycles,
unresolvable or undeclared imports, public package entry points, and the runtime-independence rules
for `run-state`. `clients/tools/tsconfig.architecture.json` maps workspace package names to their public source
entry points, so the check does not depend on prior builds. A small manifest check covers forbidden
workspace dependencies that are declared but unused, because they do not appear in a source
dependency graph. Rule regressions run as part of `pnpm test:clients`.

The check scans each package's `src/` plus the non-shipping code next to it (`tui/dev`, benchmarks,
the web end-to-end tests, and `clients/tools/scripts`). That set is derived from the workspace rather
than listed: `clients/tools/scripts/workspace_layout.mjs` enumerates the packages pnpm admits and the
source directories inside them, and the scan roots, the rule path patterns, the audited knip
workspaces, and the dependency policy all read it from there. A new package or directory is
therefore gated on its first commit. Beyond the package direction, it enforces:

- No deep imports into another workspace package (`@vibesys/x/dist/...`, `@vibesys/x/src/...`,
  relative paths into a sibling package). Only the public `exports` are importable.
- Peer frontends do not import each other. Core state remains below every frontend, and UI widgets
  receive backend-derived values through TUI-owned state instead of importing wire types directly.
- The replay harness may consume public workspace exports but may not deep-import any package's
  `src/`, including the TUI's own source tree. TUI state and controller modules do not import
  widget modules under `src/ui/`; shared pure presentation models live directly under `src/`.
- Nothing in a package imports `tui/dev`, benchmarks, or `scripts`; the tooling itself must still
  resolve, declare its dependencies, and stay free of cycles.
- Inside `tui/src`: OpenTUI is confined to `ui/` and the composition root (`index.ts`,
  `runtime.ts`); state and controller modules do not import the controller or the wiring above them;
  `ui/` reaches `session-controller` by type only and never imports the composition root;
  `index.ts` and `launcher.ts` are never imported; `launcher.ts` imports nothing but `theme.ts`.
  Test files are exempt from the `tui/src` layer rules.

## Ownership

| State or behavior | Owner |
| --- | --- |
| Generated protocol types and their validator | `contracts` |
| Framing, connection lifecycle, requests, event subscription | `transport` (Unix socket: `transport-node`) |
| Run identity, status, rounds, phases, executions, transcripts, todos, usage, benchmarks, diagnostics | `run-state` |
| Focus, selection, layout, zoom, theme, modals, drafts, query progress | `tui` |
| Terminal widgets, rendering, keyboard and mouse events | `tui` |
| Browser bindings, presentation, and browser-only interaction state | `web` |

`@vibesys/transport` is the runtime-neutral entry. It exports framing, stream policy, the control
channel, and the transport interfaces. `@vibesys/transport/websocket` contains the browser
WebSocket implementation, and `@vibesys/transport-node` the Unix-socket implementation used by
the TUI. Each entry compiles with only its runtime's library and types, so a Node builtin in
`@vibesys/transport` is a type error and a browser bundle cannot pull in `node:net`.

The backend client performs I/O and exposes validated protocol messages. Core state is a pure fold
over snapshots, ordered events, and active-execution checkpoints. The TUI owns all interaction and
presentation state, renders the combined state, and sends user intents through the backend client.

Core state latches the first non-empty `run_id` before cursor checks. Later snapshots, events, and
response events with another identity contribute one typed `run_identity_mismatch` diagnostic but
no run facts. A batch containing a rejected identity cannot update its active-execution checkpoint
or history floor, and rejected prefix events cannot contribute provenance to merged projections.
Only an explicit rebootstrap may replace the latched identity. Same-run rebootstrap preserves
snapshot-derived thread registration and TUI run-local state; a changed or unknown identity resets
those values before the replacement run is presented.

Only backend messages change core state. A frontend action may send a command, but the command does
not optimistically change backend-authoritative state. The resulting backend event does.

The web client is a presentation adapter over `run-state`. Its React external-store binding owns
subscriptions and browser presentation state; it does not fold events or copy TUI state logic.
Recorded replay fixtures are served by the development harness and folded through the same
`run-state` reducer used by live clients.

The browser launch path keeps the server composition shared. `vibesys --web` starts the existing
Unix adapter and a loopback WebSocket gateway around the same `RunApi` and
`SubscriptionTracker`; the gateway changes only framing, not request dispatch, replay, batching, or
store-identity handling. It binds `127.0.0.1`, serves the built `clients/apps/web/dist` bundle from the
same port, and prints a capability-bearing page URL. The bundle's build manifest inventories the
tracked source, configuration, entrypoint, package, and dependency-lock inputs and supplies an
opaque build ID to the instance record; the entrypoint rejects the checkout's default bundle when
that inventory is stale. Server runtime receives only
the resolved asset directory and build ID, not client-workspace layout. The first page response exchanges that launch
capability for an HttpOnly browser-session cookie and scrubs the token from browser and WebSocket
URLs. A separate-origin Vite harness retains only the minted browser-session credential in
tab-scoped storage, because strict cookies do not cross site boundaries. WebSocket handshakes
require a session credential and the exact page Origin. This is local
browser hygiene, not remote authentication. The Unix socket
and TUI remain the default path, and the WebSocket adapter uses one connection each for control,
subscription, and chat as specified by the shared wire contract.

### Detached and read-only web lifetimes

`vibesys --web --detach` is the explicit long-lived mode. The launcher creates a new session for a
server child, returns after that child publishes its capability URL, and leaves the child serving
with zero subscribers. A later TUI or browser client can use the same Unix or WebSocket bootstrap,
including the existing store-id and tail semantics. `SIGTERM` or `ServerRuntime.shutdown()` is the
deliberate stop operation. Without `--detach`, the 30-second first-subscriber timeout and
last-subscriber teardown remain unchanged.

The detached gateway publishes `.vibesys/web-gateway.json` by default. The record is written by
temporary-file replacement, has owner-only permissions, and contains the PID, loopback port,
capability token, and project root. Discovery requires both a live PID and a token-authenticated
`/health` response. A failed probe removes only the matching stale record. The path can be
overridden with `--web-instance`; it is project-local, so two working directories do not share
gateway state. The default port remains ephemeral across restarts. Use `--web-port` when a stable
bookmarkable port is required.

`vibesys --web --web-reopen PATH` serves a completed `run-events.jsonl` through the same API and
WebSocket transport without attaching a project writer. Event history and indexed state are read
from the existing event store, query bookkeeping is suppressed, and control or thread-creation
requests return the typed `run_read_only` diagnostic. A reopened server remains alive until
explicitly stopped. Read-only attach derives one non-empty recorded `run_id` from the indexed
journal and validates any explicit identity against it. Mixed recorded identities are rejected;
an identity-less legacy log requires an explicit identity. This validation uses the event index and
does not turn replay into an eager full-journal read.

`run-state` has no Node runtime, OpenTUI, theme, layout, focus, or query-result dependencies. Its
time-dependent selectors require an explicit clock value so tests remain deterministic. Transcript
labels and tones are semantic annotations derived from event fields; the TUI decides whether and how
to display them.

Experiment entries currently come from `query.experiments`. The event stream supplies an
`experiments_changed` invalidation, not the entries themselves. Query progress and results therefore
remain outside core state until the backend event contract becomes complete enough to project them.

## Launch sequence

`vibesys` spawns the server and the frontend concurrently. The launcher does not wait for the
control socket: the backend client retries `ENOENT` and `ECONNREFUSED` until its connect deadline,
so the frontend pays its own startup while the backend is still coming up. The launcher still
watches for the socket appearing, which is what distinguishes a backend that died before it ever
listened (report its log tail) from a run that failed later (the frontend already shows the
diagnostic).

Configuration stays in the backend. Without `--theme`, the frontend asks `query.tui_defaults` while
the renderer starts and applies the answer before the first frame, falling back to the default theme
if the backend does not answer in time. `--theme` skips the query and reaches the frontend as
`VIBESYS_THEME`.

### Headless detached runs and the live-instance registry

`vibesys --detach` without `--web` starts a run that outlives the shell or SSH session that started
it. The launcher validates the run arguments and fixes the run's id: parsing allocates a fresh
run's id, or resolves a resume (`latest` included) to the run it names. It then spawns
`entrypoints.detached_run` with `--run-id` in a new session (no controlling terminal, stdin from
`/dev/null`), waits until the child's control socket accepts connections, and prints the child's
`LiveInstanceRecord` as one JSON line on stdout. The child takes its registry lock and publishes a
record naming its run before it imports the engine, then serves the run through
`entrypoints.server`; every event the server journals names that run, from the first. The server
keeps running with or without clients and exits when the run ends; a finished run is reopened
read-only from its stored events with `--web-reopen`.

Each detached server registers in a per-user, node-local registry owned by `server.registry` (its
record and result models are in `server.host_api`; both stay light, see
`tests/architecture/test_light_entrypoints.py`):

```text
$XDG_RUNTIME_DIR/vibesys/            (else /tmp/vibesys-<uid>/), mode 0700
  instances/<id>.lock                lifetime lock, published only once held
  instances/<id>.json                LiveInstanceRecord, mode 0600
  runs/<id>/control.sock             the server's Unix control socket
  runs/<id>/server.log               the server's stdout and stderr
```

A record (version 4) always names its `run_id` and its run's `phase`: the `RunStatus` with the
startup `stage` (`resolving`, `building_image`, `starting_container`) and the stage's `progress`
(`done`, `total`, `label`) while `starting`, or the failure `reason` (`code`, `stage`, `message`)
once `failed`. The server folds the phase from the `run_status_changed` and `run_startup_progress`
events and the diagnostic of the failure event it journals, and rewrites its record atomically on
each change, writing a change of progress alone at most once a second. A reader upgrades a
version 3 record, whose phase is a version 4 phase. `log_path` is the
server's log. `vibesys_root` is the absolute path of the VibeSys source checkout the server runs
from (the directory whose `pyproject.toml` declares the `vibesys` project), or `null` for a server
run from an installed distribution. The desktop app uses it to suggest the checkout to run
`vibesys` from on that host.

When a server exits, on any path, it leaves its last record, with the phase its run ended in and
`ended_at`, as a tombstone; a server that dies without one (`kill -9`) is reported, and recorded,
as `failed` with reason `server_crashed`. `vibesys instances list` reports tombstones under `ended`
for 24 hours, and the next listing after that removes them. A run that fails before any client
attaches is therefore still visible, with its reason, to a client that looks later.

Records carry their own `version`, because two checkouts on one host share one registry. A lister
parses the version it knows strictly; a live server's record that it cannot read (another version,
or corrupt) is listed under `unreadable` with its id and claimed version, never dropped and never a
failure of the listing. A newer checkout's tombstone is left for that checkout's lister to age.

The root is never under `$HOME`, which may be on NFS. Liveness is proven, not read from the record:
the server holds an exclusive `flock` on its lock file for its lifetime, and the kernel releases it
on any exit, `kill -9` included. `vibesys instances list [--json]` reports a server as live only
while its lock is held; once the lock is free it reports the server's tombstone and, after a day,
removes its files. The web gateway's project-local
record keeps its own format (`server.transport.discovery`); its claim and startup-log hold use the
same lock primitives from `server.registry`.

#### Stopping and resuming a detached run

`vibesys instances stop <id> [--json] [--force]` sends the existing `command.stop` over the
server's control socket, the same request an attached client sends, so the run stops at its next
controlled boundary with its state persisted, on every platform. It then waits, bounded, for the
lock to drop. Only a server that does not acknowledge (wedged, or not yet listening), or a
`--force` stop, is sent `SIGTERM`, through a stable process reference and only while the lock is
still held; hosts without pidfds (macOS) report `unsupported` for that fallback. `--json` prints
one `InstanceStopResult`:

| `outcome` | Exit | Meaning |
| --- | --- | --- |
| `stopped` | 0 | The server stopped; its record is now a tombstone. |
| `stopping` | 0 | The server accepted the stop; its active agent call outlasted the wait, and it exits on its own after it. Poll `instances list`. |
| `not_running` | 1 | No live server holds the id. |
| `still_running` | 1 | The server was signalled and still held its lock when the wait ended. |
| `unsupported` | 1 | The socket did not answer and this host cannot signal safely. |

`route` is `control_socket`, `signal`, or `null` when nothing was sent.

A client attached when the run stops reads `run_status_changed` to `stopping`, then to `stopped`,
and then the connection closes; no `run_finished`, `run_failed`, or `run_interrupted` follows. A
server that ends with its run waits, bounded, until every open stream has written the latest
event before it closes its transport, so the terminal status arrives before the close.

`vibesys --detach --resume [RUN]` resumes a crashed or failed run as a new detached server,
through the same resume resolution as `vibesys --resume`. The child runs under the interpreter of
the `vibesys` command that was invoked, so the run continues on that command's code (a desktop
client chooses the checkout or install per host). On success stdout is the new server's
`LiveInstanceRecord` and the exit status is 0; clients attach to its socket and receive the run's
history through the usual subscribe backfill. On failure stdout is one `DetachedLaunchFailure`
line (`"outcome": "failed"`), stderr repeats the message, and the exit status is its `exit_code`.
`code` is the run's configuration diagnostic code (for example `invalid_arguments` or
`resume_not_found`, exit 2) or one the launcher owns (exit 1): `registry_unavailable`,
`run_already_live` (with the `live_instance` already driving the run, to attach to instead), or
`server_start_failed` (with the server's `log_path`).

### Boot trace

Boot timings are always recorded and never narrated. The backend times its boot in spans
(`src/vibesys/boot_trace.py`): the dispatch preamble in `src/entrypoints/headless.py`, then
run-context assembly in
`run/resources.py`. Every span lands in the run's `run-*.log` as
`boot span <qualified.name>: <ms>ms`, with the preamble's spans ahead of assembly's and each
enclosing span reporting its region's total after its children.

Nothing reaches stderr unless you ask:

```bash
VIBESYS_BOOT_TRACE=1 vibesys --input ... 2>trace.log
```

The CLI passes the request to every process it spawns, so the same variable also switches on the
frontend's own measurement of how long the landing view waits for experiments
(`clients/tui/src/boot-trace.ts`), which spans the request, the backend gate, and the reply. Those
client lines are anchored to `VIBESYS_LAUNCH_START_MS`, which the CLI always sets, so they report
wall time since the user ran the command rather than since the frontend process started.

## Validation

Run all package checks from `clients/`, the TypeScript workspace root:

```bash
cd clients
pnpm check:ts-architecture
pnpm check:knip
pnpm check:clients
pnpm test:clients
pnpm build:clients
```

`pnpm check:knip` (knip, configured in `clients/tools/knip.config.ts`) fails on unused files, exports,
dependencies, and unlisted or unresolved imports. Entry points are the package `bin` and `exports`
plus the declared test, harness, and benchmark files; an export used nowhere in the workspace should
lose its `export` or be deleted. `libs/contracts/src/generated/` is ignored because the generator
exports every schema type, and the `index.ts` of each library package is its public API. Add an
entry point to `tools/knip.config.ts` (with a comment saying who runs it) rather than suppressing a
finding. The config keys its per-package policy off the derived workspace layout, so a package
without one fails the gate by name instead of falling back to knip's default heuristics.

Each package declares its own `check`, `test`, and `build` scripts, and the workspace scripts run
them with `pnpm -r`, which covers every member in dependency order. A package that does not declare
one is reported by `pnpm check:ts-architecture`, because `pnpm -r` would skip it silently. Package
builds consume only public workspace exports. The release build uses the same dependency-aware build chain before pnpm
deploys the self-contained TUI payload.

Biome fails production TypeScript files above 2,000 counted lines and reports a non-blocking warning
above 1,500, so a split can be planned before the hard limit. Test files keep the function-length
exemption needed for fixture-heavy suites, but have an explicit 10,000-line file cap. The warning
pass excludes tests and runs as part of `pnpm check:ts`.

Biome also enforces the presentational layer. Production files under any `ui/` directory of a
client package may not call `fetch`, open a `WebSocket`/`EventSource`/`XMLHttpRequest`, use
`useEffect`/`useLayoutEffect`/`useInsertionEffect`, schedule timers, or import
`@vibesys/transport`, `@vibesys/transport-node`, `electron`, or `node:*`. The GritQL plugins are in
`clients/tools/lint-rules/` and are attached to `ui/` globs in `clients/biome.json`;
`clients/tools/scripts/check_ui_no_effects.test.mjs` is their test. The timer rule excludes five OpenTUI
widgets that own spinner or elapsed-time refresh (`activity-bar`, `agent-map`, `chat-composer`,
`experiment-log`, `round-rail`), listed by path in `biome.json`; moving those timers behind an
injected scheduler removes the exclusions.

### Regression tests for rendering bugs

Pick the test layer by where the symptom lives, and require that the test fails at the merge base:

| Symptom | Layer |
| --- | --- |
| String formatting or truncation arithmetic | Pure function test on the formatter |
| Width, alignment, clipping, padding, or overlap (anything stated in columns or rows) | `createTestRenderer` from `@opentui/core/testing`: build the real view, render at a fixed size, and measure the emitted renderables (see `clients/tui/src/ui/app.test.ts` and `todo-strip.test.ts`) |
| Theme contrast and legibility | Pure computation over theme tokens |
| Wide characters, resize, streaming timing, keybindings through a PTY | The tmux harness in the `hunt-client-bugs` skill; not a CI regression test |

The test renderer runs the real renderable tree and Yoga layout, so it reproduces layout-class bugs
deterministically in CI. It does not run a terminal: text is measured in code units, and there is
no PTY, input, or timing. A pure-function test that computes the expected width itself cannot
reproduce a layout bug and is not accepted as evidence for one.

To exercise rendering by hand without a backend, a provider, or tokens, replay a recorded run through
the development harness described in
[`clients/tui/dev/README.md`](https://github.com/uw-syfi/vibesys/blob/main/clients/tui/dev/README.md).
The harness applies the same legacy translation the server's read path applies, so a capture recorded
before `execution_id` existed replays with its agent executions intact, as a client would receive it.
