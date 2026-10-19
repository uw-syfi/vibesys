# Desktop connection architecture

This document owns how the desktop app (`clients/apps/desktop-main`) reaches VibeSys on a host:
the one program it runs there, the one way it connects, how a run is identified and how its
lifecycle is reported, and how the app's screens hold state. It is written as the design. Parts
not yet built are marked in the [status table](#status); the [migration plan](#migration-plan)
says what each phase replaces.

It builds on, and does not restate:

- [`wire-protocol.md`](wire-protocol.md): framing and connection semantics of the run protocol
  (`WP-*` decisions). Every run connection described here carries that protocol unchanged.
- [`desktop-development.md`](desktop-development.md): how to start the app, the page's
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
|     link (ssh master | none)              |  (ssh  |   -> server.host_api   (contracts)  |
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
| Screen stores | the desktop features (`clients/features/*`) and `clients/apps/desktop-shell` | One store per screen; pure reducer; React renders state. |
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
[desktop-development.md](desktop-development.md)); locally it is a direct `spawn` with an argument vector.
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
| logs | one per host, opened when the first log view comes on screen | `logs.list`, `logs.read`, and `logs.cancel` only; see [Logs](#logs). |

A control channel normally exits `0`. If a successful `run.start` reply cannot reach the client,
the helper owns stopping that undisclosed instance. Only `stopped` and `not_running` release that
ownership as success. A thrown stop, a refused cleanup worker, or any nonterminal result
(`stopping`, `still_running`, or `unsupported`) ends the channel with status `12` and writes one
typed `RunStartCompensationFailure` line per affected instance on stderr. Each line preserves the
request id, instance id, failure kind, detail, and the complete `InstanceStopResult` when one
exists, so the app or an operator can recover an instance the failed stdout reply never disclosed.

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
cost is a startup paid on attach, not per action. Both kinds of channel are opened by one function
(`HostConnection.open(kind)`), which performs the bootstrap and `hello` and returns a typed channel
or a typed failure; there is no other way to reach a host.

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
`invalid_request`, with its `request_id` when it has one. An error's `message` is for a person who
may not own the account, so the helper writes each path under its home directory as `~/...` in it
(`server.host_helper_requests.home_relative`, applied to every error the channel sends); `path`
stays as it is, and a client never shows it.

| Method | Request | Result | Notes |
| --- | --- | --- | --- |
| `hello` | `client_versions: [min, max]`, `features: [Feature]`, `app_version` | `protocol_version`, `features`, `vibesys_version`, `checkout: {root, commit, dirty} \| null` | First message on every channel; anything else first is `handshake_required`. |
| `instances.list` | none | `InstanceList`: `live` (each with `paused`), `ended` (tombstones with phase, reason and `end`), `unreadable` (id and record version) | Replaces `vibesys instances list --json`. A client that negotiates no `instances_watch` takes exactly one snapshot while installing each control channel; it never polls. |
| `instances.watch` | none | `watch_id`, then `instances.changed {watch_id, listing}` pushes: the listing at once, then on each change | Removes manual refresh (Recent and host panels). Gated by feature `instances_watch`. The helper reads the registry once a second per watch, without writing to it; the client never polls. |
| `instances.unwatch` | `watch_id` | `watch_id` | Ends a watch; no push for it follows. `unknown_watch` when none is open. |
| `tasks.list` | `project` | `TaskList` | Replaces `vibesys tasks --json`. Errors are typed: `project_missing`, `project_uninitialized`, `task_invalid`, each with the path. |
| `runs.list` | `project`, `digest_runs: [run_id] \| null` (up to 100; feature `run_digest`) | the project's runs (`run_id`, `task`, `display_name`, `created_at`) with the `phase` of the server on this host that drives or last drove each, and its `instance_id` while live; for each run named in `digest_runs`, a `digest` | Gives Recent rows status and lets resume name a concrete run. A run whose server left no record here has no phase: the registry keeps a tombstone for a day, so after that only the run's journal says how it ended. The `digest` is what a list row shows of a run, folded from its journal in one pass that decodes only the few events that carry it (`server.run_digest`), never a replay: `{kind: digest, ideas, adopted_ratio, best_so_far_ratio, duration_s, ending, closing}` (ideas are counted as the run page counts them, one per hypothesis, probes excluded; `adopted_ratio` is the result the run's adoption span names as a multiple of the input, as an improvement for a `min` objective, null when the run adopted nothing: never a failed idea's reading; `best_so_far_ratio` is what the page headlines while the run works, before any adoption exists: the best idea that passed and beat the input, worded the same way, null before one does and once an adoption exists; `duration_s` runs from the journal's first event to its last; `ending` is the run's `run_ending` record, `closing` its terminal event, both null for a run still going or whose server died without a word), or `{kind: unreadable, message}` for a journal that exists and cannot be read. A run recorded in the checkout whose journal is not in this project's log store has no digest (an empty one would read as a run that recorded nothing and stopped), so the client calls it gone. A client names only the runs it shows; a run the project does not have is skipped. The helper keeps an ended run's digest while its journal keeps its size and modification time. A client that did not ask never sees the field. With `with_dashboard: true` (feature `run_dashboard`, after `digest_runs`) each digest also carries `dashboard` and each run an `attention`. `dashboard` is the tile's data from the same journal pass: `points` are the run page's performance chart (`performanceFold`), one per idea in the order its result landed, `{ratio, verdict}` with `ratio` worded like `adopted_ratio` (null before the idea has a reading) and `verdict` one of `kept` (passed and beat the best so far and the input: on the step line), `not_better`, `failed`, `running`; at most 64 points, and past that the oldest ideas that are not `kept` go first, then the oldest `kept` ones, with `points_omitted` counting them; `failed_streak` counts the ideas at the end that failed with none passing since; `last_event_at` is the journal's last event; `best` is `{value, metric, unit}`, the reading of the last `kept` idea in the run's own metric (the objective's id and unit from `run_started`, either may be null), present only when such an idea has a reading: it is what a tile says when no ratio to the input can be worded. `attention` is one of `none`, `failed` (the terminal event, the ending or the server's phase says the run failed), `stalled` (a `running` server and a journal silent for 30 minutes, judged against the host clock the reply is built at), `repeated_failure` (a `running` server and the last 3 ideas failed). |
| `runs.read_events` | `project`, `run_id`, `from_sequence` (from 1), `max` (1 to 2000; a helper without `run_history_pages` serves up to 500), `with_totals` (optional) | `events: [RunEvent as JSON]`, `next_sequence`, `complete`, and with `with_totals` `total_events` and `total_bytes` | A run's recorded events, live or ended, read from its journal (`run-events.jsonl` in the run's log directory) so an ended run opens with its map, overview and scrubber; the live path keeps `run.connect`. Oldest first, at most `max` events and 2 MiB of journal per page (an event longer than that alone comes alone, so every read progresses); `complete` says the page reached the end of what is recorded now, and `next_sequence` is the next page's `from_sequence`. A line that does not decode (a live writer's unfinished tail) ends the journal there. The client narrows each event with the run protocol's generated `RunEvent`. `unknown_run` names a run the project does not have; `project_missing` as for `runs.list`; a run that journaled nothing has no events. Gated by feature `run_history`; `with_totals` (the journal's complete lines and their bytes when the reply was made, for a progress bar) and pages over 500 events need `run_history_pages`. The helper looks the run up once per channel, not per page. The reader decodes each line with `vs_decode` on the helper's light import path, so `hello` stays light; a line longer than a helper line (1 MiB) ends the readable journal. |
| `runs.events_for_span` | `project`, `run_id`, `span_id` (a run-trace span), `from_sequence` (from 1, default 1), `max` (1 to 500) | `events: [RunEvent as JSON]`, `next_sequence`, `complete`, `total` | The recorded events of one span and of every span beneath it, oldest first: an event belongs to the span it names in `span_id` and, when it names none, to the span whose `invocation` is its `execution_id` (those parents and invocations are the ones the journal's span events record). For an agent turn that is its output, tool calls and results, activity and usage; for a flow, those of every turn under it; span events themselves are not included, the trace already carries them. Paged as `runs.read_events` is (`max` events, 512 KiB of journal, one event alone when it is over), from the journal's span index (`run-events.jsonl.spans`, below), so a span deep in a long run costs its own events and not the run's. `total` is how many events the span has now, for a progress count; a span the journal never names has none and is not an error. Errors as `runs.read_events`. Gated by feature `run_span_events`. |
| `runs.read_tail` | `project`, `run_id`, `before_sequence` (from 1, or `null` for the journal's end), `tail` (1 to 500) | `events: [RunEvent as JSON]`, `complete`, `last_sequence` | The newest events with a sequence below `before_sequence`, oldest first, at most `tail` and 512 KiB of journal (one alone when it is over). `complete` says no older event is recorded: the page starts at the journal's first. The next older page asks for `before_sequence` of this page's first event, so pages concatenate without gaps or duplicates. `last_sequence` is the journal's newest sequence when the reply was made, whatever the page, for a client that then follows the run live from it. Gated by feature `run_tail`. |
| `runs.summary` | `project`, `run_id`, `offset` (from 0) | one page of the run's summary: `through_sequence`, `event_count`, `journal_bytes`, `trace`, `rounds_finished`, `span_count`, `reply_count`, `usage_count`, `record_count`, `tokens`, `overview`, `input_gates` and `terminal` (with `with_ending`), `spans`, `replies` (each with a `schema_id`), `schemas`, `usage`, `records`, `next_offset` | Everything the run level shows, as of the journal's end now, without reading the journal. The summary is the client's own fold of the journal to `through_sequence`: the run trace (`foldTrace`: each span as its latest event left it, in the order the spans started), the overview of the last `run_started` that declares one (`foldOverview`; no unit or direction a live server adds from run state), each agent turn's last typed reply (the transcript fold's reply entries, which `implementerOutcomes` reads), and the run's spend folded from `turn_usage_recorded` by summing (`ledgerWith`): `tokens` over every turn, those a span names and those none does, and `usage` (a `TokenTally` per turn span, in the order the spans first spent), and `records`, one `UsageRecord` per `turn_usage_recorded` event in journal order (`server.run_usage`: the turn joined to the `agent_execution_started` of its `execution_id`, so it names the role, harness, model and reasoning effort that ran it; a turn whose start is missing has those as `null`, never inferred). The records' tokens sum to `tokens`, and `tests/conformance/fixtures/usage-records` holds a journal and the records both readers must agree on. A count the provider did not report is `null`, never zero, as in the event; a sum is `null` until some turn reports the count. `trace` says whether the journal records a run trace at all: `predates_trace` when rounds finished (`rounds_finished` counts the `round_finished` events) and no span event was ever recorded, a journal written before the trace existed, whose ideas and measurements cannot be read (its `spans` are missing, not empty) and which the Run screen says were recorded by an older VibeSys; `runs.list`'s digest reports `ideas: null` for the same journals, by the same rule (`trace_format`). `input_gates` are the `gate_finished` events before the first workstream span (`foldInputGates`: a gate's `gate`, `recipe`, `passed`, and `output_tail`, the end of its redacted output cut to 4000 characters, from which the client reads the short reason as it does for a journal; at most 32), and `terminal` is the last `run_finished`, `run_failed` or `run_interrupted` event with its whole `message` (cut only at 16000 characters, where the end record's `reason` is one line cut at 200); both repeat on every page, and a run opened from its summary shows its failed input gates and the whole reason from them. The idea rows, measurement points and chart are folds of those spans and replies the client already owns (`CandidateFold`, `performanceFold`), so nothing is defined twice; `tests/conformance/fixtures/run-summary` holds journals and the summary both folds must produce, checked by the helper's tests and by `run-summary-conformance.test.ts`. An event at or before the last folded sequence is skipped whole, as the client skips it. Items (every span, then every reply, then every span's usage, then every record) come in pages of at most 512 KiB: start at `offset` 0 and continue at `next_offset` until it is `null`; a page carries the schemas of its replies that no earlier reply uses (`schemas`, by `TurnReply.schema_id`, the SHA-256 of the schema's canonical JSON) and the overview and total `tokens`, so it stands alone. The events after `through_sequence` are the live tail (`runs.read_events` from `through_sequence + 1`, or a subscription after it). A run still writing can move between pages: every page names its `through_sequence`, and a read whose pages disagree starts again. The fold is kept in `run-summary.json` beside the journal, a disposable cache the journal can always rebuild: it records the journal byte offset it covers (the end of a complete line) and digests of the journal's first 4 KiB and of the 64 KiB before that offset, and is used only while the journal still begins with the bytes it covered, so a read folds only what the journal gained (a 600-idea journal of 891 MB: 1.0 s cold, 30 ms warm, 0.15 s after 300 more events, on a developer laptop). A torn last line (a writer's crash mid-append) is no event until it is finished and is never cached; a resume appends after the last complete line; a journal replaced by another file, a cache of another fold version or a damaged one is rebuilt from the journal; a cache that cannot be written costs only time. `unknown_run` and `project_missing` as for `runs.read_events`; `unknown_run` also for a recorded run whose journal is not in this project's log store (an empty summary would read as an empty run). Gated by feature `run_trace` (the reply names `trace` and `rounds_finished`: a reply that gains a field gets a feature of its own, so a helper from before it is refused by the client before it answers in a shape the client cannot read); `offset` needs `run_summary`, and `with_ending` needs `run_ending` (a client sends it only to a helper that offers it, and reads a summary without the two fields as one without gates or a terminal event). |
| `run.start` | `project`, `task`, `args: [str]`, `resume: {run_id} \| {latest: true} \| null`, `harness: {provider, model} \| null`, `options: {rounds, time_limit_s, results: keep \| publish_github, roles: [{role, model \| null, reasoning_effort: default \| low \| medium \| high}]} \| null`, `agents: {ROLE: {provider, model}} \| null`, `measure_on: MeasurementTarget \| null` | `{instance_id, run_id, socket_path, phase}` or `DetachedLaunchFailure` | Allocates or resolves `run_id` before detaching (see [Run identity](#run-identity)); returns once the record is published, not when the run is ready. `args` holding `--allow-unvetted-model`, or any abbreviation argparse accepts, is `invalid_request`: a run started here never opts out of the vetted harnesses. `harness` (gated by feature `harnesses`; absent or null keeps the project's configuration) must be a pair `harnesses.list` offers, else `unvetted_harness` naming the provider's vetted models; it becomes `--cli-provider` and `--model`, so `args` may then hold neither. `options` (gated by feature `run_options`; every field optional, unset keeps the configuration) become the run's own flags: `rounds` is `--max-rounds`, `time_limit_s` is `--max-run-seconds` (overriding `[run].max_run_seconds`), `results` is `--local` (keep) or `--publish` (publish under the generated repository name), and each role is `--role-model ROLE=MODEL` and `--role-reasoning-effort ROLE=EFFORT` (overriding `agent.roles.ROLE`). `args` holding a flag of an option that is set, or an abbreviation of one, is `invalid_request` naming it (`--repo` counts as a results flag). With `harness`, a role's model must be one of its vetted models, else `unvetted_harness`; without one, the run's own validation names `agent.roles.ROLE.model`. `agents` (gated by feature `run_role_agents`; absent, null or empty changes nothing) names the agent each role runs: each key is a role id (what `run.preflight` returns with `with_roles`) and each value a pair `harnesses.list` offers, so a role starts with its own provider and model while a role with no entry uses `harness` (or the project's configuration). Each entry becomes `--role-provider ROLE=PROVIDER` and `--role-model ROLE=MODEL` (overriding `agent.roles.ROLE.provider` and `.model`; a role on a provider other than the run's and with no model runs that provider's default model). A key that is not a role id is rejected when the message is read, naming `agents.KEY`. A pair `harnesses.list` does not offer is `unvetted_harness` naming `agents.ROLE` and the provider's vetted models. A role the run's orchestration does not declare is `invalid_request` naming `agents.ROLE`: the helper asks the run's own validation (`vibesys preflight`, with the arguments the start would launch) before it launches, only when `agents` has entries. A role set in `agents` and also given a `model` in `options.roles` is `invalid_request` naming both keys (its reasoning effort may still be set there), and `args` may not repeat `--role-provider` or `--role-model` (or an abbreviation of either). A run environment that starts containers carries the login of the run's provider only, so roles on more than one provider fail the run's settings check there (`config_invalid`), until containers carry each provider's login. `measure_on` (gated by feature `measurement_target`; absent or null changes nothing) says where results are measured: `{kind: same_machine}`, `{kind: slurm, cluster}` (a cluster `compute.list` lists) or `{kind: modal}` (a token present on the machine). It maps onto the existing run-environment wiring and adds no second path: the same machine adds no flag, so the run's own environment choice stands (including the host environment where that is selectable); a cluster is `--run-environment slurm --slurm-config PATH`, PATH being the operator file of that cluster (a cluster of the helper's compute file is rendered to its own file there on every use, and the operator's `slurm.toml` is used as it is); Modal is `--run-environment modal`. A cluster not set up on the machine is `unknown_cluster`, and Modal without a token `modal_not_configured`, before anything launches. With `measure_on`, `args` holding `--run-environment`, `--slurm-config`, `--modal` or `--skypilot` (or an abbreviation of one) is `invalid_request` naming it, so the target has one source. |
| `run.preflight` | `project`, `task`, `args`, `harness`, `options`, `agents`, `measure_on` (as `run.start`), `with_roles: bool \| null` (feature `run_roles`), `with_backend: bool \| null` (feature `run_backend`), `with_project_clean: bool \| null` (feature `project_clean`), `with_causes: bool \| null` (feature `run_causes`) | `checks: [{id, state: ok \| warn \| fail, detail, fix: Fix \| null, cause?: Cause}]`, one per `id` in order: `checkout_found`, `project_own_repository`, `project_clean` (only with `with_project_clean`), `settings_valid`, `docker_reachable`, `agent_image`, `evaluator_available`; `Fix` is `{kind: choose_checkout} \| {kind: copy_out, suggested_path} \| {kind: edit_setting, key} \| {kind: start_docker} \| {kind: declare_build, manifest, outputs}` | Start is enabled exactly when no check is `fail`. The arguments are refused as `run.start` refuses them. `settings_valid`, `agent_image` and `evaluator_available` come from `vibesys preflight ARGS`, run in the project with exactly the arguments `run.start` would launch (same interpreter, same directory): it runs the run's own `validated_run_request` (the configuration, the task, the orchestration's options and roles), so a run that passes does not fail its configuration at launch, and a failure's detail is the diagnostic's first line, naming the key. `settings_valid` also fails (code `accelerator_unavailable`) when the run's environment hands its agent the compute backend's accelerator on this machine and the machine cannot provide it, or when a docker-in-docker task needs a Sysbox runtime that the machine's Docker daemon does not register, the checks the run makes before its image build (`vibesys.api.request.run_host_shortfall`, with the run's own message); a failure of the run's backend is also a typed `cause`. The run's backend is its task's (`backends` in the task manifest; see [running-vibesys](../running-vibesys.md#compute-backend)), so the failure is a fact about the task and the machine, never advice to change a setting. `evaluator_available` fails with `declare_build` (the task manifest and the outputs) when the evaluator package requires candidate build outputs (`candidate_outputs`) and the task declares no `[build]`; the app offers Check again after the manifest is edited. `agent_image` is `ok` when an agent image was built on the machine before (a build reuses its layers) and `warn` that one will be built otherwise. `project_own_repository` fails, with `copy_out` beside the containing repository, when the project sits inside another Git repository. `docker_reachable` fails with `start_docker`. Both `docker_reachable` and `agent_image` apply only when the run's resolved run environment starts local Docker containers (`RunEnvironment.uses_docker`, which `vibesys preflight` reports): for one that does not (the host environment), each is `ok` with a `not needed` detail, so every check id is always present and an unreachable Docker never blocks such a run. When the settings resolve no environment, Docker is checked. Gated by feature `preflight`. With `with_roles` set, the reply also carries `roles: [str]`, the role ids the run's orchestration plugin declares (`AgentRole.id`, in declaration order): what a `RoleOption.role` may name, and the ids `--role-model` is checked against. They come from the same `vibesys preflight` run, which reads them from the plugin the run's arguments select (`PreflightReport.roles`), so the plugin's declaration is the only source. `roles` is empty when the settings resolved no orchestration, and absent when `with_roles` was not set, so a strict client that did not ask never sees the field. Gated by feature `run_roles`; a helper without it reports no roles and a client lists none. With `with_backend` set, the reply also carries `backend: cpu \| cuda \| rocm \| metal \| trainium`, the compute backend the run resolved to (`PreflightReport.backend`; absent when the settings resolved none), and a failing check carries its typed `cause` when it has one: `{kind: backend_unavailable, task, backend, declared: [backend]}` for a task whose backend this machine cannot provide (`settings_valid`, code `accelerator_unavailable`). The check's `detail` words the same fact in one line naming the task and never a flag or a setting; the CLI words it with `--backend` in the report's `settings.message`, and a client words `cause` for its own controls (the New run screen shows the backend read-only beside the task and tells the user to choose another task or machine). Neither field is present when `with_backend` was not set, so a strict client that did not ask never sees them. Gated by feature `run_backend`; a helper without it reports no backend and a client shows none. With `with_project_clean` set, the checks also include `project_clean` (after `project_own_repository`): it fails when the project is its own Git repository with changes a new run cannot start beside, with the typed `cause` `{kind: project_dirty, project, paths: [str]}`, the same set the run's own tracker refuses at launch (`GitTracker.baseline_changes`, read by `vibesys preflight` into `PreflightReport.project_changes`; VibeSys's own run state is not counted; nothing is reported for a project that is not a repository yet, or for a resume). Its `detail` is one plain line; the client words the cause (the project's name, up to five paths, and Check again). A client that did not ask never sees the check, so the checks it knows stay one per id. Gated by feature `project_clean`. With `with_causes` set, a failed `settings_valid` carries the typed `cause` of every other way the run's configuration can fail, a closed set (`server.host_preflight.Cause`; the facts live in `vibesys.api.ConfigurationCause`, which the CLI words with its flags): `task_not_found {task, available}`, `task_unusable {task, problem}`, `task_needs_provisioning {task}`, `task_image_unsupported {task, problem}`, `sysbox_missing {task, problem}`, `config_invalid {problem}`, `roles_not_declared {roles}`, `model_not_vetted {provider, model, vetted, role, fallback}`, `arguments_rejected {problem}`, `arguments_unrecognized {arguments}`, `option_not_accepted {option: rounds \| time_limit \| results \| role_model \| role_reasoning_effort \| role_agent}` (the helper traces an argument the run rejected back to the typed option that wrote it) and `github_unavailable {problem}` (a run that publishes, checked ahead of Start through the run's own owner resolution). `problem` and `arguments` are the run's raw text: the client shows them only in a collapsible detail, never as a headline, and the check's `detail` carries none of it and names no flag. A failure with no cause (or a client that did not ask) keeps the run's first message line as `detail`; the client words such a failure generically and keeps that line as the detail. Gated by feature `run_causes`. With `measure_on` set (feature `measurement_target`), the arguments `vibesys preflight` is run with carry the target's environment flags as `run.start` would launch them, and the reply ends with one more check, `measurement_target`: `ok` when the target is set up and its hardware runs the task's backend (the same machine always passes here, since `settings_valid` already answers for it), `warn` when the target is set up but its hardware or the task's backend is not known, and `fail` with a typed `cause` when it is not set up (`{kind: measurement_target_not_configured, target}` with the fix `{kind: configure_compute, target}`: a cluster the machine does not list, or Modal without a token) or does not offer the task's backend (`{kind: measurement_target_misfit, target, backend, offered}`; Modal offers `cuda`). A client that sent no `measure_on` never sees the check, so the checks it knows stay one per id. |
| `harnesses.list` | `with_generations: bool \| null` (feature `harness_generations`) | `harnesses: [{provider, model, default, display: {provider_name, model_name, family, tier, tier_name}}]`, providers whose CLI is installed on the machine first (`machine.describe`'s `harness_clis`), each provider's default first | The vetted provider and model pairs (`vs_agent`'s `harnesses()`), for the start sheet's model picker. `display` is how agentshim names the pair: `provider_name` ("Claude Code") always, and `model_name` ("Opus 5.5"), `family` ("Claude"), `tier` (rank in the family, 1 the smallest) and `tier_name` ("Opus") when agentshim names the model (`model_name` and `family` null otherwise, `tier` and `tier_name` null off the ladder); a client renders them and shows the raw id for a null name. With `with_generations` set, `display` also carries `maker` (`anthropic \| openai \| google`, the closed set of makers; null when agentshim names no maker) and `generation` (`{id, name, order}`: the generation the model belongs to within its maker, `name` as people call it ("GPT-6"), `order` its rank among the maker's generations with 1 the newest; null when agentshim names none, so a client groups such a model by `family` alone), so the screen groups maker, generation and models without reading a name or an id, and older generations stay selectable while a harness serves them. Within a maker one generation id has one name and one order and no two ids share an order, or the reply is not sent. Neither field is present when `with_generations` was not set, so a strict client that did not ask never sees them. Gated by feature `harnesses`. The helper loads the agent library only when this or a `run.start` with `harness` arrives, so `hello` stays light. |
| `machine.describe` | none | `machine: {checkout: {root, version, commit, dirty} \| null, hardware: {cpu_cores, gpus: [{vendor, model}], accelerator: cpu \| cuda \| rocm \| metal}, docker: {reachable: true, version} \| {reachable: false, reason}, harness_clis: [provider], runs: {live, ended_today}}` | For the Machines and New run screens. `checkout` is the helper's own (what starts runs here), `null` from an installed distribution. GPUs come from the first vendor tool that lists any (`nvidia-smi`, `rocm-smi`, Apple Silicon's CPU brand), which names `accelerator`; `cpu` exactly when none is listed. `docker` asks the daemon for its server version, so `reachable` proves the daemon answers; `reason` is one line. `harness_clis` are the vetted providers whose CLI program is on the helper's search path, in offer order. `ended_today` counts tombstones since the host's local midnight. Each fact's command has 5 s; one that cannot run counts as absent. Gated by feature `machine_describe`; the agent library loads only when it is asked, so `hello` stays light. |
| `checkout.inspect` | `path` | `inspection: {state: found, root, version, commit, dirty, compatible} \| {state: not_found} \| {state: too_old, version, min}` | Whether a path (`~` and a relative path as for `paths.complete`) holds a VibeSys checkout: its `pyproject.toml` names `vibesys`. `too_old` when its version's release segment is below `MIN_CHECKOUT_VERSION` (or it declares none, `version` empty); `compatible` says it ships the host helper the app starts from a checkout. Path completion stays `paths.complete`. Gated by feature `checkout_inspect`. |
| `examples.list` | none | `listing: {version, checkout, examples: [{name, category, tasks, external_repo}]}` | The catalog (`examples/registry.toml`) of the checkout this helper runs from, the same document `vibesys example list --json` prints. `examples_unavailable` from an installed distribution or an invalid catalog. Gated by feature `examples`. |
| `compute.list` | none | `settings: {config_path, clusters: [{cluster: SlurmClusterSettings, source: vibesys \| slurm_toml}], modal: {token_present, source: environment \| profile \| null}}` | What the machine the helper runs on offers as places to measure. The settings are the machine's, not the client's: the Slurm clusters of its compute file (`~/.config/vibesys/compute.toml`, named by `config_path`, the one file the helper edits) and, as `source: slurm_toml`, the cluster of the operator `~/.config/vibesys/slurm.toml` a terminal run reads with `--slurm-config` (listed and never written, so existing Slurm configuration is reused, not copied). `SlurmClusterSettings` is `{name, reach: {kind: ssh, host} \| {kind: login_node}, workspace, partition, nodes, time_limit, sbatch_options: [str], hardware: {accelerator: cpu \| cuda \| rocm \| metal \| null, gpu_models, gpus_per_node} \| null}`: `ssh` is a host alias in the machine's `~/.ssh/config`, `login_node` that the machine is itself the cluster's login node, and `hardware` null until stated or read from `sinfo`. `modal` says whether a Modal token is present (the id and secret variables, or a profile of `~/.modal.toml`) and where, never the token. A compute file with an unknown key or version is `compute_config_unreadable` naming its path. Gated by feature `machine_compute`. |
| `compute.test_cluster` | `cluster: SlurmClusterSettings` | `result: {outcome: passed, hardware, partitions} \| {outcome: failed, stage: reach \| scheduler \| workspace, detail}` | Test a connection from the machine without saving: over `ssh` (batch mode, bounded) or locally on a login node, `true`, then `sinfo -h -o '%P\|%D\|%G'` (limited to the cluster's partition when it names one), then a check that the workspace exists and is writable. `hardware` is read from `sinfo`'s GRES column (the most GPUs any node lists, every model named, its accelerator when every model runs on one; `accelerator: null` for GPUs `sinfo` does not type), so a client fills the hardware from it. The commands run through the command-probe interface, so a deterministic test drives them with a Fake. Settings the Slurm configuration rejects (a time limit it cannot read, a relative workspace, `--requeue`) are `cluster_invalid` naming the keys to edit. |
| `compute.add_cluster` | `cluster: SlurmClusterSettings` | `settings` as `compute.list` | Save a cluster to the compute file once it passes the same test again, so a connection that must pass before saving is enforced where the file is written. `cluster_exists` for a name already set up in either file, `cluster_invalid` as above, `cluster_test_failed` naming the stage and detail, in which case nothing is written. |
| `compute.remove_cluster` | `name` | `settings` as `compute.list` | Forget a cluster of the compute file. `unknown_cluster` for a name not set up, `cluster_read_only` for the operator file's cluster, which only its operator edits. |
| `agent_logins.list` | none | `logins: [{provider, provider_name, installed, state: signed_in \| signed_out \| unknown, how?: {kind: account} \| {kind: api_key, variable?, file?}, api_key_variables: [str], detail, can_start_login}]`, one per provider `harnesses.list` offers, in its order | Whether each vetted harness's CLI is installed on the machine the helper runs on, whether it is signed in, and how. Every fact is agentshim's, read through `vs_agent.provider_login` (`probe_provider` for the CLI's own status, the profile's credential sources for how): `state` is the CLI's own answer, `unknown` when it is not installed, offers no status command or the command could not run, so a client does not report that as a problem. `how` is present only when signed in and the CLI said: an `account` login, or an `api_key` named by where it is held, `variable` (an environment variable) or `file` (a home-relative path), never the key. `api_key_variables` are the variables that would sign the CLI in with a key, so the app can say what to set. `detail` is the CLI's own line. `can_start_login` says whether `agent_logins.start` can run this harness's login (see below). Gated by feature `agent_logins`. |
| `agent_logins.start` | `provider` | `result: {outcome: started, instruction?} \| {outcome: unavailable, reason: not_installed \| no_login_command \| needs_terminal \| start_failed, command?, detail}` | Ask the helper to start the harness's own login on the machine. `unknown_provider` for a provider `agent_logins.list` does not list. The command is agentshim's (`ProviderProfile.login`); `vs_agent.provider_login_command` picks the device-code variant when the CLI has one, since it finishes on a machine with no browser. The helper runs it as a child that outlives the request and answers once the CLI has printed its page and code: `started` with `instruction`, one line ("Open URL and enter the code CODE"), absent when the CLI showed none within the bound; the child is ended after 15 minutes if the person never finishes. A login that prompts the person (Claude Code, opencode) is not run: `needs_terminal` with `command`, the line to type in a terminal. `no_login_command` is a CLI with no sign-in command (Gemini CLI), `start_failed` a command that could not run or ended with a complaint and no page (`detail` is its first line). `can_start_login` is true exactly for an installed harness with a command that needs no terminal. Gated by feature `agent_logins`. |
| `usage.window` | `project`, `run_ids` (up to 100), `since`, `until` (timezone-aware, `since` before `until`, at most 336 hours apart) | `spent_usd`, `spend_per_hour_usd`, `tokens`, `turns`, `unpriced_turns`, `untimed_turns`, `new_bests`, `cost_per_new_best_usd` (null when no best landed), `by_run: [{run_id, task, cost_usd}]` (costliest first), `by_agent: [{role, harness, model, cost_usd, turns}]` (costliest first, at most 64, `agents_omitted` counts the rest), `missing_runs` | What a project's runs spent in `[since, until)`, for the Usage page. A turn is placed at the time it ended and priced by its reported cost, from the run's usage records (`server.run_usage`, the records `runs.summary` serves, read from the summary cache beside the journal), so nothing is re-derived. `until` is "now": the helper reads no clock, the client names it. `spend_per_hour_usd` is the cost in the last hour of the window (all of a shorter window) per hour. A new best is a `kept` idea of the run's digest chart (`IdeaPoint.at`, the journal time of its result) that landed in the window; `cost_per_new_best_usd` is `spent_usd` over them. A turn that reported no cost is `unpriced_turns` and one with no end time `untimed_turns`, so a total is a lower bound. A run the project does not have, or whose journal this machine does not hold, is in `missing_runs`. The work is bounded by the run cap and each run's cached fold, and the reply by 100 runs and 64 agents (a test holds the largest under a quarter of the line limit). Gated by feature `usage_window`. |
| `project.create_from_example` | `example`, `destination` | `created: {project, tasks}` | Copies the example's tracked files (or clones an external example at its pinned commit) into its own new Git repository with one commit, as `vibesys example new` does. `destination` (as typed, like `paths.complete`) must be absent or an empty directory (`destination_exists`) and outside the checkout (`destination_nested`); `unknown_example` lists the matches for an ambiguous or unknown name; a failed copy is `project_create_failed` and leaves nothing behind. Each error names its path. Gated by feature `examples`. |
| `examples.destination` | `example`, `destination` (as for `project.create_from_example`) | `state: absent \| empty \| example_project \| occupied`, `project: str \| null` | What `project.create_from_example` would find at `destination`: nothing (`absent`), an empty directory (`empty`; both are created), a project with the example's tasks (`example_project`, with its resolved directory in `project`, which a run can use as it is), or anything else (`occupied`: another project, a directory with entries, a file). An example whose source is another repository that is not fetched has no tasks to compare, so nothing is its project. `examples_unavailable` and `unknown_example` as for `project.create_from_example`. Gated by feature `example_destination`. |
| `project.copy_out` | `source`, `destination` | `created: {project, tasks}` | Makes a folder that sits inside another Git repository its own: copies its tracked files into a new repository at `destination` with one commit (files never added to the containing repository stay behind). `destination` must not exist (`destination_exists`) nor lie inside the containing repository (`destination_nested`); a `source` in no other repository is `not_nested`, a missing one `path_missing`. Nothing is left behind on failure; each error names its path. It is the action of `run.preflight`'s `copy_out` fix. Gated by feature `examples`. |
| `project.task_goal` | `project`, `task` | `goal: {task, objectives: [{metric, direction: max \| min}], goal, goal_truncated}` | The goal of one task of the user's own project: its `OBJECTIVE.md` (cut to 32000 characters, `goal_truncated` says whether it was) and the axes of its `objectives.toml` (none when the task has no such file), read through `vs_project`'s project layout, the same facts an example's `task_details` carries. `project_missing` and `project_uninitialized` as for `tasks.list`; a task the project does not have, or files that cannot be read, is `task_invalid` naming the cause. Gated by feature `project_goal`. |
| `run.stop` | `instance_id`, `force` | `InstanceStopResult` | Same outcomes as `vibesys instances stop`. |
| `run.reader` | `project`, `run_id` | `{instance_id, read_only}` | The server Chat reads a run through, which `run.connect` then opens as it does a live run's (`chat` role). A run with a live server is read through it (`read_only` false). A run with none is read through a read-only server: the run server in its read-only mode (`vibesys --detach --reopen`) over the run's journal, started on demand, shared by every client that asks while it lives, and ended by itself once no client has held a connection to it for 60 seconds (nothing the helper does, and no helper that dies, leaves one behind). It registers apart from the runs (`readers/` beside `instances/` in the registry root), so `instances.list`, `runs.list` and `run.stop` never see it and it never blocks a resume of its run. It answers reads and refuses every command, a steer included, with the typed diagnostic `run_read_only` (a client words it). `unknown_run` for a run the project does not have; `reader_failed` when the server did not start, and the next ask tries again. Gated by feature `chat_ended_runs`. |
| `run.connect` | `instance_id`, `role: control \| subscribe \| chat` | `{run_id, protocol_version}` then relay | Run channels only; `channel_busy` while another request or watch is open. The run's `run_id` comes back here, so the page never derives it. Bytes the client sends before the reply are relayed after it. A failed connect (`run_gone`, `connect_denied`, `connect_failed`) leaves the channel a request channel. |
| `ping` | none | an empty reply | Control channel liveness for the supervisor. |
| `logs.list` | `instance_id`, `span_id` | `streams: [{id, owner, command, retained_start, size, outcome}]`, oldest first | The run's recorded log streams, live or ended. `span_id` (gated by feature `logs_by_span`; absent or null lists every stream) keeps only the streams whose owner names that run-trace span. `owner` is the JSON object the writer validated against vibesys's `LogOwner` union (the client narrows it with that generated type); `outcome` is `ended`, `failed`, `abandoned`, or `null` while open. `unknown_instance` when the host knows nothing of the instance; `log_store_corrupt` when its run's index does not decode. Gated by feature `logs`. |
| `logs.read` | `instance_id`, `stream_id`, `start: {offset} \| {tail_bytes}`, `max_bytes` (4 to 64 KiB), `wait_ms` (0 to 30 s) | `chunk: {offset, data, next_offset, eof, closed, truncated_before, pipes: [{offset, length, pipe}]}` | One bounded chunk; at the end of an open stream it waits up to `wait_ms` for bytes. `unknown_log_stream`, `unknown_instance`, `log_store_corrupt` (the run's index does not decode; a retry fails the same way). Gated by feature `logs`. See [Logs](#logs). |
| `logs.cancel` | `read_id` | `read_id` | Withdraw the `logs.read` whose request id is `read_id`: its wait ends at once, it answers what it holds, and this reply follows that answer, so the read's worker is free. Admitted even when the channel's workers are full. Naming no waiting read is not an error and answers at once. Gated by feature `logs_cancel`. See [Logs](#logs). |
| `paths.complete` | `prefix` | `directory`, `entries: [{name, path, vibesys_checkout, vibesys_project, git_repository}]`, `truncated` | For the host's project and checkout pickers. `~` expands on the host and a relative prefix is under the home directory; a trailing `/` lists the directory. Directories only (a link counts when this account can follow it to one), filtered by the typed name prefix exactly, sorted, at most 200. A directory is read for at most 10,000 entries; past either bound `truncated` is set. `path_missing`, `path_not_directory`, `permission_denied` name the path. Gated by feature `paths_complete`. |

There is no `projects.recent` method: Recent is the app's own record of what this user opened,
across hosts, and has one writer (the settings store). What the host knows, the runs of a project
and their status, is `runs.list`. See [Decisions and alternatives](#decisions-and-alternatives).

`run.stop` needs the run protocol's `command.stop` and `Response`, and `Response`'s payload types
import the engine. The run protocol's control envelope (the requests and responses `run.stop`
needs) is therefore split from the engine-importing modules first, as step one of phase 4 (see
[Phases](#phases)), so the helper imports only the envelope and the import guard has no exceptions.

### Logs

The `logs` feature lets the app show, live, the output of long-running work on a host (image
builds, container start, evaluator stages, the run's own narration). The run records each stream
under a typed owner in its `vs_logs` store ([logs.md](logs.md)); the helper reads that store and
serves it in bounded chunks. Nothing about logs rides the run's event stream.

- **Where the helper reads.** `server.host_helper_logs` answers the methods over one role,
  `LogReader`, which opens an instance's logs (`InstanceLogs`: list its streams, read raw bytes).
  A request opens them once, so a waiting read re-reads only its stream, not the instance's
  inspection. `server.host_helper_os.StoreLogReader`
  implements it over `vs_logs.read`: it finds the instance's run from its live record or, once the
  run ended, its retained inspection metadata (`inspection.json`), and opens the store at
  `Project.log_streams_directory_for(project_root, run_id)`. So a run's logs stay readable after it
  ends, and the helper keeps no state between requests. A helper with no log store does not offer
  `logs` in `hello`.
- **Tail first, then follow.** A view opens with `{tail_bytes}` (64 KiB), then reads forward from
  `next_offset` with `wait_ms`. At the end of an open stream the helper re-reads every 250 ms on its
  injected clock and answers as soon as bytes arrive, the stream closes, or the wait ends; the wait
  always ends 1 s before the request's deadline, so a long poll is answered, never
  `deadline_exceeded`. Nothing is fetched for a stream that is not on screen, and nothing is
  replayed from offset 0 unless asked.
- **Withdrawing a read.** A waiting read holds one of the channel's 16 request workers for up to
  its `wait_ms` (25 s from the app's follower). A follower that stops (its view left the screen, or
  the Run screen's Logs tab switched streams) sends `logs.cancel {read_id}`: the helper sets the read's
  withdrawal, its wait ends at once, the read answers what it holds (the client already dropped
  it), and the cancel's reply follows, so a worker never outlives the follower that asked for it.
  Without it, switching streams more than 16 times within a wait filled the channel and new reads
  got `channel_busy`. The method is behind its own feature, `logs_cancel`, not `logs`: a helper
  that offers only `logs` would refuse it as `invalid_request`, so a client sends it only when
  `hello` returned the feature and otherwise just drops the read, as before. Two alternatives were
  rejected: a new `logs.read` replacing a pending one on the same stream does not help a switch to
  another stream and breaks two views of one stream; a short `wait_ms` with immediate re-issue
  still holds a worker per switch for that wait and multiplies the polling.
- **Chunk boundaries.** `data` is text: the bytes `[offset, next_offset)` decoded as UTF-8, with
  bytes that are not UTF-8 replaced by U+FFFD. When more bytes follow the window, the chunk ends after
  the window's last line end, so a view rarely has to join a line across chunks; with no line end in
  the window, it ends before the character the window splits. At the end of what the stream holds,
  the chunk keeps an unfinished last line (a progress bar, a prompt) so it shows at once, and holds
  back only an unfinished last character of an open stream until its last byte is written; at the
  end of a closed stream nothing is held back. A cut therefore never splits a character, so decoding
  chunk by chunk gives the same text as decoding the whole stream, and offsets stay byte offsets.
  `max_bytes` is at least 4, one whole character, so every read of available bytes makes progress.
  A tail that does not start the stream starts at its first line start. Encoding the bytes as base64
  instead was rejected: it costs a third more on the wire and pushes decoding, with the same
  boundary problem, into every client.
- **Evictions.** The store caps each stream and evicts its oldest bytes. A read that asks for
  evicted bytes starts at the oldest byte still held and sets `truncated_before`. `pipes` labels each
  byte with the pipe (stdout or stderr) that wrote it, as spans in stream offsets covering the chunk
  exactly.
- **Which channel.** Logs ride their own request channel, one per host, which the connection
  manager opens when the first log view comes on screen; the helper treats it like any request
  channel (no new helper channel kind). The reasons are the ones in [Stream
  topology](#stream-topology-why-one-channel-per-run-connection): a 64 KiB reply or a waiting read on
  the control channel would delay `ping` and the instance watch behind it, and long polls would hold
  control's request workers (16 per channel); on a channel of its own, ssh's per-channel window keeps
  log bulk from blocking control or the run's event stream. One logs channel per host, not one per
  stream: replies are bounded and served concurrently, and each follower keeps exactly one read in
  flight, so a chatty stream delays another by at most one chunk, while a channel per stream would
  cost a helper process each and count against `MaxSessions`. The 16 workers bound the streams one
  host follows at once, more than a screen shows.
- **The client.** `@vibesys/logs` holds the data fold (`foldChunk`: chunks into a bounded line view
  with the unfinished last line, the evicted range, and the closed flag) and the control state
  (`logFollowMachine`: idle, tailing, following with one read in flight, closed, and error with
  retry on the injected clock), over a `LogTransport` the composition provides on the logs channel.
  A view starts a follower when its stream comes on screen and stops it when it leaves. Stopping
  aborts the read in flight, and the abort withdraws it on the helper: `HelperChannel` sends
  `logs.cancel` for an aborted `logs.read` when `hello` negotiated `logs_cancel`.
- **The channel's lifetime.** `HostConnection.openLogs()` hands out leases on the host's one logs
  channel. The first request of any lease opens it once the host is ready (a host whose control
  `hello` did not negotiate `logs` answers `feature_not_negotiated` and opens nothing), every lease
  shares it, the next request reopens it if it ended, and releasing the last lease closes it.
- **From the Run screen.** The Run screen's Logs tab shows a run's logs, live or ended (the store
  outlives the run). It names its host and instance on the shell channel `vibesys:welcome:logs`
  (a listing, or a read that must name that instance, or a `withdraw` of an aborted read's token:
  an IPC request cannot be withdrawn by itself, so each read carries a token the page chose, and an
  aborted read sends a `withdraw` request naming it), answered by the hosts feature's main-process
  `Hosts.logs`. `Hosts` keeps one connection lease and one logs lease per renderer document, on one
  host at a time: the first request takes them, asking about another host swaps them, and leaving
  the run (`vibesys:welcome:release-logs`) or the document going away releases them. The tab lists
  the streams grouped by owner kind and follows only the one on screen; narration streams render
  as records by level with their traceback, raw streams as monospace with stderr marked
  (`foldChunk` keeps each line's pipe runs). The page drops an aborted read's late answer.
- **Not `server.log`.** `server.log` is the detached server's raw stdout and stderr, for a person at
  a terminal; no UI feature depends on it. `instances.inspect`'s `include_log` (a tail of it) is
  deprecated: the app no longer sends it, and refuses a reply that carries a tail. The helper keeps
  answering it for older clients and removes it at helper protocol version 2.

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
   so a strict peer never sees what it does not know. `HELPER_REQUEST_FEATURES` (methods) and
   `HELPER_FIELD_FEATURES` (optional fields, gated when set) in `server.host_api` are the gates;
   the generated schema exports both, and the helper, the client channel, and the `FakeHelper`
   each refuse an ungated use as `feature_not_negotiated`. An error code only a gated request can
   produce (`unvetted_harness`) needs no version bump either. This replaces probe-by-rejection
   (`WP-CAPABILITY-PROBE`) for the helper protocol; the run protocol keeps its probe until it gains
   a `hello` of its own.
   A new field on an existing *reply* is no exception: the client parses replies strictly too, so
   a helper from before the field would end its channel as `incompatible`. The method then takes
   a feature of its own (`runs.summary` needs `run_trace` for `trace` and `rounds_finished`).
   The features the app cannot be used without are one list, `REQUIRED_HELPER_FEATURES`
   (`@vibesys/hosts`): a machine whose `hello` lacks one is `needs-update` in Machines, Where and
   Review in New run (Start is disabled with that reason) and on the Run screen, before any run is
   asked of it. A feature enters the list when a screen starts to depend on it.
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
| `starting {stage, progress}` | `stage` is `resolving`, `probing_workspace` (a Slurm run proving its workspace root), `building_image`, `building_tools` (evaluator tools the cache lacks), or `starting_container`. `progress` is `{done, total \| null, label}` when the stage reports it. | no |
| `running` | Ready and executing (`RunStatus.RUNNING`; also `pausing`, `paused` as today). | no |
| `stopping` | A stop was accepted; the run ends at its next boundary. | no |
| `stopped` | Stopped by request; its result and measurements are kept, and it has ended for good: not resumable (Pause is how a run continues later). | yes |
| `failed {reason}` | `reason` is a `Diagnostic` (code, stage, message, log path). | yes |
| `finished` | Completed (`RunStatus.COMPLETED`). | yes |

This extends `RunStatus` rather than adding a parallel enum: `starting` gains the startup stage and
progress, `failed` gains its reason, and the record's `phase` is a projection of the same fold that
emits `run_status_changed`, so the two cannot disagree.

Facts enter once:

- **Journal**: a new event `run_startup_progress {stage, progress, state, condition, elapsed_seconds}` and the existing
  `run_status_changed` (now carrying the reason on `failed`). The agent image build
  (`vs_agent.api.images.agent_image`) parses Docker's progress output into typed step
  observations (`ImageBuildProgress {done, total, label}`), not log lines; the run environment
  reports them, and the stage it is in, to the request's `startup` sink (`vs_runtime`), and the
  engine emits each as a `run_startup_progress` core event. The run's status stays `starting`
  until its session reports it ready (`RunTrigger.READY`); attaching the journal is not readiness.
  Each stage journals `state` `running` when it begins (and at each `progress` step), `waiting` while
  blocked on the scheduler (`condition` `queued` or `running`, `elapsed_seconds` waited so far, once per
  poll), and `finished` with the `elapsed_seconds` it took; a stage that fails journals no `finished`.
  An image build reports its last step, `exporting to image` (`done` equal to `total`), when Docker
  writes the image out, which for a large base image takes minutes after the last Dockerfile step.
  Only `running` events move the run's stage in the record. Every container the run starts, the first
  and the candidates' after it, is named once by a narration line carrying the container's id and the
  session's role, never by a repeated identical line.
  A pause or stop requested while starting is held (`RunState.requested`), not entered: the run
  keeps reporting `starting` with its stage and progress, a resume drops the held request, and
  `READY` lands the run in `pausing` or `stopping` if one is still held. A pause lands (`paused`)
  once core holds the run paused and nothing of it is running: at the end of the agent turn in
  flight, or at once when only a measurement job is (the job finishes, and what the search then
  decides waits for the resume). Core may land a held stop
  before `READY`, abandoning the startup; the run then goes from `starting` straight to `stopped`. A failure while starting past `resolving` names the startup stage it failed in.
- **Starting label**: the run view's status chip shows the stage, its progress, how long the
  stage has run, and, once nothing has been heard for 60 s, `quiet for <duration>`, so a slow
  image build or container start never looks frozen. The fold keeps the envelope timestamps of the
  stage's first event (`stageSince`, reset when the stage changes) and of the latest one
  (`lastActivityAt`); the label is a pure function of that state and a `now` the page owner
  ticks once a second only while the run is `starting`. It uses no event beyond
  `run_startup_progress`.
- **Record**: the server rewrites its record atomically (temp file, rename) on each phase change;
  a change of progress alone is written at most once per second, the latest one when the second
  ends.
- **Paused**: the live record's `paused` is `user` or `quota` exactly while `phase` is `paused`
  (record version 5; a record upgraded from version 4 has none). The run server writes it from its journal: a `quota_paused` event names the
  cause of the pause that follows it, until the run leaves its pause; any other pause is the
  operator's. `instances.watch` therefore drives the sidebar's paused status and the "paused"
  notification without a run connection.
- **Ended records**: when a server exits, its last record (terminal phase and reason) stays in the
  registry as a tombstone for 24 hours, with `end {outcome, reason, resumable, resume_from_round}`
  (record version 5). `outcome` is vs-core's `EndReason` (`adopted`, `kept-input`, `no-winner`,
  `adoption-failed`, `stopped`, `deadline`, `finish-refused`) when the run recorded a result, else
  `force_stopped` when the server was terminated, `stopped` for a stop that left work unsettled,
  and `failed` otherwise. `reason` is one line (at most 200 characters): core's account of the
  result, else the failure's message. `resumable` is whether the run's durable core record exists
  and is not terminal and no stop ended it: a run that recorded a result is not resumable, and
  neither is any stopped run (`stopped`, including one whose drain outlived its grace bound, and
  `force_stopped`), since a stop ends a run for good. `resume_from_round` is then its finished
  rounds plus one. A force stop records the stop request in the run's state
  (`ProjectState.record_stop_request`) before the server is signalled, so a resume of a
  force-stopped run commits that stop first, starts no new work, and only finishes the drain and
  ends; `vibesys --resume` refuses a run whose record is terminal (`run_ended`, "run ended; start
  a new run"). A core run reports these as the `run_ending` event just
  before it ends; a run that failed before core started reports none and is not resumable. A
  crashed server's burial records `failed`, resumable, with no round. `end` is absent on a
  tombstone upgraded from version 4 and when a completed run reported no ending, so the UI falls
  back to the phase; the next listing after that removes it, with no coupling to
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
- **Record versions**: a reader reads versions 3, 4 and 5, upgrading each step strictly (a
  version 4 document carrying `paused` or `end` is `unreadable`). Version 5 changed the helper's
  `InstanceList` within helper protocol version 1, as version 4 did, because no helper client has
  shipped; once one has, a record change needs a helper version or feature.
- **Accepted gaps across record versions and crashes**:
  - A tombstone of a newer record version is never aged by an older reader: it is reported
    `unreadable` and left for that version's lister to remove, so a host that only ever runs the
    older checkout keeps it.
  - A crashed run's `ended_at` is when a writing listing found it dead, not when it died: the
    server wrote nothing as it died, and the lock carries no time. Its 24 hours run from then.

The UI renders only the phase: Retry appears only on a link failure, never on an ended phase;
Resume appears only on an ended run whose `end` says `resumable` (a failure or a crash), never on a stopped or finished one.

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
   `instances.list`). Prompt authority belongs to the live callers of one link attempt, not its
   first caller: an explicit sign-in that joins a master check makes the resulting master
   interactive; if a batch master is already starting, its exact process retires before one
   interactive successor starts. A sign-in abandoned before launch cannot authorize a prompt.
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

## Screen stores

Every screen (Settings, Machines, New run, the Run screen, the sidebar) has one store:

- **One discriminated union per screen.** For example New run's tasks are
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
  no state that changes behavior. The desktop shell is React.
- **Native `<dialog>`.** Sheets are `<dialog>` opened with `showModal()`: focus containment, inert
  background, Esc, and return of focus to the opener come from the platform.
- **Data from watches.** Host panels and Recent statuses fold `instances.watch` pushes; there is no
  manual refresh path to forget.

The desktop window (`apps/desktop-shell`, the only renderer since the cutover, #2850) follows the same rules: the sidebar
store (`@vibesys/feature-app-shell`) folds the listings, Recent and the archived runs, and the
Settings store (`@vibesys/feature-settings`) saves each change under a token, so a slow save
cannot undo a newer choice. Both have invariants checked over seeded action sequences.

The New run screen (`@vibesys/feature-new-run`) is an XState machine: its role table lists the roles
the latest preflight reported (a helper without `run_roles` gets an empty state instead), and its
preflight checks are
asked for the inputs' key and kept only for that key, so Start is enabled only when the checks
shown are for the inputs now and none fails, and a reply for older inputs is never shown. Its
model test checks that, and that an input never restarts what the machine offers. The run's compute
backend is its task's: the screen shows what the checks reported (`run_backend`) read-only beside the
task, never as an input, and words a typed `cause` for its own controls.

The Machines store (`@vibesys/feature-machines`) keeps the saved machines and derives each one's
status from its last report rather than storing one: `Sign in` when the connection waits for
credentials, `Checkout not found` when the bootstrap reports no
VibeSys checkout at the saved path (the connection manager's `incompatible` phase carries that as
a typed `checkout` problem, `missing` or `too-old`), `Can't connect` for any other failure, and
otherwise `Ready` with what `machine.describe` says. Adding a machine or changing its checkout is
the hosts feature's `setCheckout`, which starts the helper from the path before it saves it; path
suggestions are `paths.complete`, each listed checkout labelled with its version by
`checkout.inspect`, so they need a helper already running on that host (This Mac, or a machine
whose checkout works). Its invariants are checked over seeded action sequences.

The Run screen (`@vibesys/feature-run-page`) has one store per shown run, a reducer over the run's
registry record (from the host's watch, through the sidebar's listings) and its log streams. The
phase is the record's: the screen never shows a phase the host did not report, except that a Stop
it sent shows as stopping until the record says so. Stopping, by this window or anyone else, arms a
two-minute bound (`FORCE_STOP_BOUND_MS`) on the shell's injected clock under a request token;
"Force stop" is offered only when that token returns while the run is still stopping, so a late
bound for an earlier stop is dropped. The screen's Stop confirmation states what is lost (a
stopped run ends for good; Pause is the resumable path), so it sends `stopConfirmed`, which runs
the same stop flow without the app's native question; the stop flow lets a view force an accepted
graceful stop. The ended banner reads the end record and offers Resume, naming
`resume_from_round`, only when it says `resumable`. Logs are grouped by their owner (startup
steps, gate stages, tool builds, narration) and read over the welcome bridge's `logs`, which the
main process serves from the host's logs channel until the screen lets it go; the startup
timeline is built from the startup steps' streams. "Open in new window" opens the run in a
window of its own (see "Windows and notifications").

A live run is attached to the window under a token (the store's `link`), through the same
`RunControl.attach`. The window binds that run to its surface itself (`AppWindow`): no other page
opens, the run IPC registry admits the shell document's connects, and the wake goes to it. The shell's `connectRun` hands main one end of a new
channel and the page the other, as a window message (`RUN_PORT_MESSAGE`), because a port cannot
cross the context bridge; the page speaks the relay's credit protocol over it
(`@vibesys/ipc`'s `openPortSocket`) and the app shapes it as the session's transport. While the
run is attached, the store keeps its `WebSession` open (bootstrap, stream, recovery stay in
`@vibesys/run-session`'s machine) and folds what it reports: the session's state, the run fold, and
the trace's accepted span events in sequence order (the session's `onTracePublication`), so the
tabs fold the trace at the sequence they show (`viewedRun`). Pause and Resume are protocol commands
on that session, offered only while its controls are up. When a rebootstrap cannot prove that the
current trace base still belongs to the run, its reset initiates a snapshot request fenced to the
established store, including when that store is unchanged. A run paused by a usage limit shows the quota banner
instead of the header's Resume: the provider and its reset time, "Resume now" (`command.resume`
with `when: now`), "Wait and resume when quota is back" (`when: capacity_returns`), and, once the
run reports `quota_waiting` (folded into the run fold's quota pause, with its `resumes_at`), the
expected time and "Stop waiting" (`command.pause`) until `quota_wait_cancelled`. A server that
refuses the wait (an older one rejects `when`) no longer offers it for that run instance. Times are
words for the events' epoch seconds in the user's time zone; no clock is read. Leaving the run
releases it (`releaseRun`), and a new shell document detaches the run the old one held. An ended run
opens from its summary: the store asks for `runs.summary` (features `run_summary` and `run_trace`, through the welcome
bridge's `readSummary`, whose main-process reader joins the pages and reads again when the run moved
between two), seeds the trace from it as it does from a live session's snapshot (`traceStep`), and
folds the turns' last replies and the run's spend into the core from events rebuilt from the summary
(`run-summary.ts`), so the run level is final and interactive without reading the journal and the
header, the hero and the idea rows read what they read for the whole history. A live run takes it
for its first paint until its session's snapshot arrives. The transcript, the agents' models and a
turn's tool output are in the history: a pane that needs them (an agent or a step is open, or the
Agents face) asks for it (`need-history`), and it is read as below into a core of its own that
replaces the seed only when it is all read; until then those panes show its progress and the run
level stays. A host without the summary, or one whose summary read fails, is read through its
history from the start, behind the loading screen. Only if that read also fails does the screen
report an unavailable run:
the store reads `runs.read_events` (feature `run_history`,
through the welcome bridge's `readEvents`) with up to three pages in flight under tokens (the first
open-ended; its reply's totals cut the rest of the journal into spans, `history-pager.ts`), validates
each event, and folds the pages in journal order, each event once, into the same `run` slice, so the tabs have one source; an older host answers
`unsupported`. While the pages are read the screen shows the read's progress, not the fold, since a
prefix of the history is not the run (`historyLoading`). Every window's `readEvents` is answered by
the main process's `RunHistoryStore` (`@vibesys/run-history`): the one copy of an ended run's pages,
kept per (host, project, run) within a bound of 256 MiB (least recently read run dropped first, a
run larger than the bound read through), shared by every window, and read from the host once for
all windows that ask while it is in flight. A second window or a reopened run is answered from it
without a request to the host. The store forgets a run the host lists as live again (a resume) and
every run of a host that goes away; failures and `unsupported` are never kept. It holds the host's
replies as typed raw data (never a fold or a view model), keyed by the request that produced them, so
later tiers add reads of other ranges or executions as further methods of the same store. Its invariants (force offered only while stopping and only after its bound, no stop
flow outliving the live run, the link holding exactly the live server, no session report without an
attached run, trace events in increasing sequence, commands sent only when offered) are checked over
seeded action sequences with stale replies. The theme is in the first paint: the welcome preload asks
the main process synchronously for the stored theme and the window controls' inset
(`FIRST_PAINT_CHANNEL`, one fact from `window-chrome.ts`) and sets them on the root element before
the page's scripts run.

## Windows and notifications

The app has one main window and, for each run the user opens with "Open in new window" (the Run
screen's "⋯" menu and the sidebar row menu), one run window that shows the Run screen for that
run alone, with no sidebar (`RunWindow` in `apps/desktop-shell`, chosen by the run in the page's
query). Opening a run that already has a window focuses it. `RunWindows` (`run-windows.ts`) is the
registry, keyed by run, and `openRunWindow` on the welcome bridge is its request.

Each `AppWindow` holds one surface (the shell page) and binds to at most one run at a time. `RunControl` and
`RunIpcRegistry` key every binding, navigation claim, and IPC admission by `run_id` rather than by
one shared slot (`clients/features/run-control/src/main/run-control.ts`,
`clients/apps/desktop-main/src/main/run-ipc.ts`), so windows attached to different runs cannot
cross-detach or cross-admit each other's connection. `RunControl`'s `openPage` binds a run to the
window showing it (`RunWindows.showing`) and any other run to the main window, so a run with a
window of its own is live there. Closing a run window forgets it and detaches only that run's live
connection; the run itself goes on. That no window steals another's port or run is checked by
`run-ipc.test.ts` (the registry), `run-control.test.ts` (independent bindings) and `desktop.test.ts`
(a run window's surface answered until it closes).

The welcome IPC has one handler per channel, because Electron allows one. `WelcomeRouter`
registers each channel once and routes every request to the window whose current welcome document
sent it, on that window's page; a sender that is no window's current document is refused. Each
window has its own admission (document generations, replacement barrier, retirement), which ends
when its native window closes. Pushed events (listings, recent runs, preferences) go to every
window's surface.

A launch from the command line (`--instance ID [--host NAME]`, or `--project PATH`) opens its run in
a run window: `Desktop.open` attaches (or starts) the run to learn its identity, lets the binding go,
and opens the run window, whose own shell document attaches it. A bare `--host NAME` is refused
(it needs `--instance`), since the shell has no host panel to open.

Known gaps: in a run window "Run again" only focuses the main window (`show-main-window`) instead
of opening New run with the run's inputs there, and a notification click focuses the main window
rather than the window of the run it names.

## App lifecycle

`AppLifecycle` is the one owner of process-wide state:

- **Single instance.** `app.requestSingleInstanceLock()`; a second launch focuses the first and
  forwards its arguments.
- **Quit sequence.** A pure machine `running -> quitting(releasing) -> exiting`: the first quit
  request starts a bounded release (close channels and masters), the second request or the bound
  exits. There is exactly one `app.exit` call site.
- **Settings store.** `hosts.json` and `recent.json` have one writer, the store, which serializes
  updates and replaces files atomically. With the single-instance lock, no other process writes
  them. A `recent.json` with unreadable entries is read for the others and reports each entry
  skipped; updates write each unreadable entry back as it was, beside the valid runs, so the
  user's entries are never dropped or rewritten silently and new runs still reach Recent.

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
| 6. Screen stores and app lifecycle | React screens (the welcome view, host panel, start-run sheet and run strip, replaced by the desktop shell at the cutover) on per-screen stores with tokens and `<dialog>`; `AppLifecycle` with single-instance lock, quit machine, settings store. | Model-based invariant tests per store; quit and second-launch journey steps. Can land before phase 5 against today's welcome bridge. |

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
| Connection manager (phase 5) | built: `ConnectionManager` and `HostConnection` (an XState machine, shell with disposal scopes), the bootstrap, typed channels with one strict validator, run channels, watch-pushed listings, path completion, the HostConnection contract suite over the `FakeHelper` and the real helper; not built: the Playwright journey job, a second master past `MaxSessions`, and checkout completion on a host with no checkout yet (it needs a helper) |
| Screen stores and app lifecycle (phase 6) | built (#1839) |
| Logs feature | built: `logs.list`, `logs.read`, and `logs.cancel` (feature `logs_cancel`) over the run's `vs_logs` store, the `helper-logs-*` scenarios (in process), and `@vibesys/logs` (fold and follow machine); the connection manager's logs channel, the run view's startup log panel (web), and the Run screen's Logs tab; not built: gate logs in the run view's round cards, and the logs scenarios over stdio against the real helper |

The span index (`run-events.jsonl.spans`, beside the journal) is what makes `runs.events_for_span` and
`runs.read_tail` seek instead of scan. The journal stays the only truth: the index is a disposable read
model, one line per event (`[offset, length, crc, sequence, span, execution, link]`), appended by the
run's writer right after the event, so a crash leaves it behind the journal and never ahead of it. A
reader (`server.span_index`) also checks the small source stamp (`run-events.jsonl.spans.source`), which
records the journal's file identity and stat after the writer's last sidecar update. It rejects a
same-size rewrite or replaced file, trusts an append-only extension, verifies the first and last indexed
lines, and scans the journal beyond the sidecar; a missing or damaged stamp falls back to a full scan.
Nothing writes the sidecar but the run. A journal an older store restarted numbering in reads with the
cursor the event store repairs it to, in these replies as in a subscription.

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
