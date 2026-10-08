# Live campaign telemetry: streaming a long run into the web UI

Status: draft for discussion. Owner: web + orchestration. Companion to the
campaign dashboard in PR #1304 (`feat/web-trajectory-replay-fixtures`), which
this builds on.

## Problem

A VibeSys serving-system campaign is a long run: a portfolio of concurrent
workstreams, each a hypothesis with its own implement/review/profile/evaluate
lifecycle, producing hundreds of measurements over days. PR #1304 shows what we
want to see while that runs: one performance graph of every measurement with a
running-best line, a draggable timer bar to view the run as of any moment, a
workstream timeline, and a Kanban/table drill-down. Today that dashboard is
driven by a hand-curated fixture, not live data.

The question this doc answers is narrow and load-bearing: **how do we get that
much telemetry from the backend to the browser, live, accurately, and
efficiently, without any delay, and without scattering print statements through
the orchestration code.** The fixture's own footer states exactly which parts
are real versus reconstructed; closing that gap is the work.

## Goals and non-goals

Goals:

- One typed, append-only telemetry contract that carries everything the
  dashboard renders: objective/metrics, workstream lifecycle, measurements,
  agent roster, token spend, run status.
- A live fold on the client that grows a `CampaignRecord` as deltas arrive and
  follows the newest point with no delay, while keeping the scrubber free to
  look back.
- A runnable local prototype that streams the existing campaign over this
  contract so collaborators can see and critique the live behavior, the way
  #1304 let people see the layout.

Non-goals (named here, deferred to the backend path in the last section):

- Emitting these events from the real orchestrator. That needs a projection of
  `DynamicState.workstreams` and new wire-protocol event types; it is sketched
  but not built here.
- Running a real `dynamic` campaign locally. It cannot be done: `dynamic`
  orchestration raises at startup unless the run environment supports parallel
  candidate workspaces, which is true only on Slurm and Modal
  (`orchestration/dynamic/orchestration.py:1240`,
  `vs_runtime/_workspace_resources.py:95`,
  `vs_runtime/_run_environment.py:759,1399`). So any local demo is necessarily
  replay-driven; see "Prototype".

## Background: what already exists

**Telemetry already flows through one chokepoint, not prints.** Backend events
are emitted at a single point (`WireJournal.record`), persisted append-only
(`EventStore`, JSONL), and served to the browser over a chunked websocket
(1 MiB frame cap). The event envelope carries `execution_id`, `round_label`,
`agent_kind`, and `run_id`. The frontend folds events into state with pure
reducers (`@vibesys/core-state`), and the web client already has a durable,
reconnecting stream (`PersistentEventStream`, `WebSession`). The design below
reuses that shape rather than inventing a parallel path.

**The dashboard is a pure function of `(CampaignRecord, cursor)`.** This is the
key property of #1304 and the reason live streaming is tractable. `CampaignView`
components depend only on a `CampaignRecord` (the data) and a `CampaignViewModel`
(cursor plus view state); nothing in the view talks to a fixture URL or a hook
directly. Dragging the timer bar to an earlier measurement re-derives
everything: run status flips to active, best-goodput recomputes as the
running best up to the cursor, the chart truncates, workstream counts and
timeline lanes shrink, and the Kanban recounts. "Active" is never stored; it is
derived from the cursor time versus each workstream's `finishedAt`. So **live is
not a new rendering mode. Live is: append to the record and move the cursor to
the tail.**

**Metrics already describe themselves.** A measured serving run emits the
evaluator result protocol v2: a JSONL stream of one `hello` record that declares
each metric's unit, direction, and whether it is required, then one `result`
(or `error`). A consumer learns the axis labels, units, and directions before
any value arrives. The telemetry contract below carries the same information in
its first frame, so a live chart can label its axes from the start.

## The gap

PR #1304's fixture footer names it precisely: the measurement points and their
values are recovered from a real campaign, but "workstream windows, role
assignments, and turn text are curated reconstructions from available evidence,
not backend events." Three concrete things are missing between the current
backend and a live `CampaignRecord`:

1. **Workstream state is never projected.** `dynamic` holds the full lifecycle
   in `DynamicState.workstreams` (phase, attempts, candidate, review,
   evaluation), but `dynamic/plugin.py`'s `_project()` exports only the embedded
   `HypothesisState` and discards `workstreams`. No external consumer can see
   "N workstreams in flight, in these phases" today.
2. **The event envelope has no workstream identity.** There is `round_label`
   and `execution_id`, but no `workstream_id` and no per-workstream phase/status
   event, so even the events that do flow cannot be grouped into the workstream
   lanes the UI draws.
3. **There is no token-spend field.** Token usage is tracked per invocation
   (`usage.jsonl`) but never reaches this schema; the dashboard's token column
   is permanently disabled.

Everything else the dashboard needs (measurements with values and gates,
objective and metric catalog, benchmark-version boundary) already exists as
data; it is the workstream layer and token spend that are unprojected.

## Design

### One contract: an append-only stream of typed campaign frames

Model the telemetry as a closed, discriminated union of **frames**, each a small
typed delta, emitted in one order over one connection. This is the single source
of truth the UI folds; it is transport-agnostic (the prototype serves it over
SSE, the real gateway would carry it over its existing websocket). Frame kinds:

| Frame | Carries | Maps to the gap |
| --- | --- | --- |
| `campaign-init` | id, title, summary, objective (metrics catalog, target, gates), benchmark versions + boundary | the self-describing header, like evaluator `hello` |
| `workstream-upsert` | one workstream: id, title, hypothesis, window, **phase**, outcome | gap 1 + 2 (`WorkstreamPhase`, `workstream_id`) |
| `agent-upsert` | one agent: id, name, role, workstreamIds | agent roster |
| `measurement` | one measurement: sequence, workstreamId, values, gates, disposition, benchmark version | the already-real telemetry |
| `tokens` | workstreamId, cumulative tokens | gap 3 |
| `status` | run status: active / completed | run lifecycle |

Every frame carries a monotonic `seq` for ordering and idempotent dedup. Frames
are **deltas, not snapshots**: a workstream that moves `IMPLEMENTING ->
REVIEWED` is one `workstream-upsert`, not a resend of the whole portfolio. This
is what keeps a days-long, hundreds-of-measurement run cheap to stream and cheap
to re-fold on reconnect.

`phase` is the closed set the backend already has (`WorkstreamPhase`: PENDING,
IMPLEMENTING, IMPLEMENTED, REVIEWED, EVALUATED, FAILED, PARKED, CANCELLED). The
UI derives "active vs accepted vs rejected" from phase plus outcome, instead of
the replay-only trick of comparing the cursor time to `finishedAt`.

### Parse at the boundary, typed inside

Frames arrive as untrusted input. `parseCampaignFrame(value): CampaignFrame`
validates once at the edge: it rejects an unknown `kind`, an unknown key inside a
frame, or a wrong type, and names the offending field. Nothing downstream re-
validates; the fold and the view operate on typed values only. This mirrors
`parseReplayScenario` for the static record.

### The fold is a pure functional core

`foldCampaignFrame(state, frame): FoldState` is a pure reducer: `state + event
-> new state`, no I/O, no clock, no React. `FoldState` holds the growing
`CampaignRecord`, the run status, the per-workstream token spend, and the last
applied `seq`. Properties the reducer guarantees, and the tests assert:

- Measurements are kept sorted by `sequence`, regardless of arrival order. This
  matters because `dynamic` assigns sequence numbers at planning time but
  appends round records when workstreams finish, so measurements can arrive out
  of wall-clock order.
- Upserts are keyed by id, so a re-sent workstream or agent merges, it does not
  duplicate.
- Replaying `framesFromRecord(R)` through the fold reconstructs `R`. The frame
  builder and the fold are inverses; this is the central round-trip property and
  the thing that makes the prototype a faithful reference for the real backend.

Keeping the fold pure is what lets the whole live path be tested with generated
frame sequences and no sleeps (see "Verification").

### The live view model, substitutable for the fixture hook

`CampaignViewModel` is already a declared, source-neutral interface: "a fixture
clock or a live event fold may implement this interface." The live path supplies
a second implementation, `useLiveCampaign(stream)`, substitutable for
`useCampaignHistory(record)`. `CampaignDashboard` does not change.

Two behaviors are new and specific to live:

- **Follow the tail with no delay.** As each `measurement` frame folds in, if
  the user has not scrubbed back, the cursor advances to the new newest point,
  so the graph and timer bar track the run in real time. If the user drags the
  scrubber back, following stops and the cursor stays put; dragging back to the
  tail resumes following. This is standard live-tail UX and the reason the timer
  bar stays useful during a live run. The existing play/pause control maps to
  follow/pause.
- **Status comes from the fold, not the cursor.** `useCampaignHistory` derives
  status as "cursor is not at the last index." That is wrong for a live run
  sitting at the tail while work continues. `useLiveCampaign` takes status from
  the `status` frame, so "active" means the run is actually active.

### Efficiency

- Deltas, not snapshots: O(1) bytes per state change, not O(portfolio) per tick.
- One connection, one ordered stream, folded incrementally; no polling, no
  per-widget fetches.
- The first frame carries the metric schema, so the chart needs no second round
  trip to label axes.
- Reconnect re-folds from `seq`; because upserts are keyed and measurements are
  sequence-sorted, a replay from zero and a resume from a cursor converge to the
  same state.

## Prototype: replay-as-live

Because `dynamic` cannot run locally, the prototype proves the design by
streaming the **existing** campaign over the contract above, as if live. It adds
no backend, core, or wire-protocol code, and no new dependency.

- A vite dev plugin (`campaignStreamPlugin`) serves Server-Sent Events at
  `/__vibesys/campaign/stream`. On connect it reads the campaign record, builds
  the frame sequence with `framesFromRecord`, and emits the frames on a clock:
  the init and agent roster first, then workstream, measurement, and token
  frames interleaved in wall-clock order, then a final `status: completed`.
  `?interval=<ms>` sets the per-frame delay; the server owns its timer and clears
  it on disconnect. The record is loaded through the app's own
  `parseReplayScenario` and `framesFromRecord` (via `ssrLoadModule`), so the
  prototype has one frame producer, not a duplicate. SSE is used because the
  telemetry is push-only and needs no dependency; the frame schema is identical
  over any transport.
- `EventSourceCampaignStream` is the browser transport (native `EventSource`);
  `FakeCampaignStream` is an in-memory implementation of the same interface used
  by tests.
- `main.tsx` mounts the live path on `?campaign-live`, wiring
  `useLiveCampaign` + `CampaignDashboard`.

Run it:

```bash
cd clients && pnpm --filter @vibesys/web dev
# open http://localhost:5173/?campaign-live
# pace it with the per-frame delay in ms:
#      http://localhost:5173/?campaign-live&interval=200
```

What it demonstrates: measurements appearing one by one with the graph, the
running-best line, and the timer bar growing with no delay; the status chip
going active then completed; workstream lanes and the Kanban filling as phases
advance; the scrubber pausing follow and looking back mid-stream. What it stubs:
the frames come from the curated record, not a live orchestrator; token values
are the record's reconstructed spend; `dynamic`'s real concurrency is not
exercised (it cannot be, locally).

## Backend path (future, not in this PR)

The prototype's frame schema is the proposed contract. Making it real is three
changes, in expand-migrate-contract order, each behind the existing single
emission chokepoint so no print statements are added:

1. **Project workstream state.** Extend `dynamic/plugin.py`'s `_project()` to
   emit a typed workstream-lifecycle event when a `DynamicWorkstream` changes
   phase, sourced from the state the plugin already holds. One owner, one event
   per transition.
2. **Add workstream identity to the envelope.** Add `workstream_id` to the event
   envelope so existing per-turn events group into lanes. One source of truth
   for the id (the `hypothesis_id` the plugin already keys on), generated into
   the client protocol types via `pnpm generate:protocol`, with the conformance
   corpus extended rather than per-client snapshots.
3. **Surface token spend.** Fold the per-invocation `usage.jsonl` into a
   per-workstream token total and emit it as the `tokens` frame.

The server-side campaign projection then replaces `framesFromRecord`: the same
frames, sourced from live state instead of a finished record, consumed by the
same client fold and view. The round-trip property is what guarantees the client
does not need to change when the source does.

## Design checkpoint

- **Owner.** The web client owns the live fold and view model; the frame
  contract is co-owned with orchestration (it is the projection boundary). No
  backend module changes in this PR.
- **Interface.** One new published interface, `CampaignStream`, plus a second
  implementation of the existing `CampaignViewModel`. `CampaignDashboard`'s
  surface is unchanged. The frame union and `parseCampaignFrame` are the typed
  boundary.
- **Direction.** Frames flow one way: source -> parse -> pure fold -> view
  model -> view. No view code imports the stream or the fixture. No upward
  import.
- **Coupling.** The view couples only to `CampaignRecord` + `CampaignViewModel`,
  as it already did. The live path adds no coupling to the view.
- **Twice.** The rejected alternative was per-widget live queries (each panel
  subscribes to its own feed). It couples every widget to the transport, has no
  single ordering, and cannot answer "state as of cursor T" coherently. The
  single-ordered-fold design hides more and is substitutable with the fixture.
- **Functional core.** The fold is the pure core (`state + frame -> state`); the
  stream transport and the vite SSE server are the shell; `CampaignStream` is
  the interface the shell calls; `EventSourceCampaignStream` and
  `FakeCampaignStream` are its implementations.

## Verification

- `campaign-frames.test.ts`: `parseCampaignFrame` accepts every valid frame kind
  and rejects unknown kinds, unknown keys, and wrong types, naming the offender.
- `campaign-fold.test.ts`: round-trip (`fold(framesFromRecord(R))` reconstructs
  `R`), sequence-sorting regardless of arrival order, keyed-upsert dedup,
  cumulative token spend, status transitions, idempotent replay, and the
  in-flight-workstream projection, over the campaign fixture and constructed
  frame sequences, with no sleeps.
- `e2e/campaign-live.spec.ts`: against the dev stream, measurement count and the
  chart grow over time, best-goodput updates, status goes active -> completed,
  and scrubbing back pauses follow. This is the one place the React + transport
  wiring is exercised, matching the repo's unit-core / e2e-integration split.
