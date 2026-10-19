# Flows: how a search is written

A search is a **flow**: ordinary `async def` code that reads top to bottom, using a small
vocabulary core owns. The runtime (`vs_core.flows`) journals everything a flow observes and
replays it after a restart, so a flow survives crashes, and tests drive it with scripted outcomes
in milliseconds.

The design takes its principles from Effect (effects are data the runtime interprets; three kinds
of failure; policies as values), its semantics from Trio (structured groups, shielding,
cancellation only at `await`), and from RxJS the idea that what a flow waits for is data, built
from a few combinable sources. The surface is plain Python: `async`/`await`, `async with`,
`match`, pydantic values.

## A search

```python
from vs_core.api.search import INPUT, Flow, NoWinner, search

@search("sequential", config=SequentialConfig)
async def sequential(ctx: Flow, config: SequentialConfig) -> NoWinner | None:
    ...
```

`@search(name, config=Model)` registers the root flow and returns a `SearchDef`. There is nothing
else to declare: core derives what it needs from the registration (the operations the search's
namespace owns, the lifecycle capabilities it may use, the schema of its persisted state).
There is no event list, no "uses" list and no state class. A search that asks agents also passes
`roles=` (its `Role` declarations; the protocol and the reply schema table derive from them) and
`operations=` (the namespace's operation shapes: verify, interpret, retain, render; these are not
role data). A search whose flows act on a system another library owns (an issue board) passes
`owned=` (the codecs of that library's operations); `ctx.owners.perform` sends one and returns the
typed outcome or a `Failed`, and the declaration requires the host to provide an owner for each.
A search that labels its spans passes `spans=` (its `SpanKind` declarations; see "Span labels").
The hypothesis searches share one vocabulary, `SPANS` in `vibesys.search_spans` (planning calls,
workstreams, agent turns, gates, the adoption), which the run page reads; each maps its own steps
onto it: the dynamic search in `src/vibesys/orchestration/dynamic/search/_spans.py`, the serial
searches (single, multi; each round is a workstream) in `src/vibesys/serial_search/_spans.py`,
evolve (each child is a workstream) in `src/vibesys/orchestration/evolve/search/_spans.py` and the
issue queue (each job is a workstream) in
`src/vibesys/orchestration/issue_queue/search/_spans.py`.
The id scheme derives from the search name.

The root flow starts with the config and returns `None` or `NoWinner(reason)`. **A flow never
finishes the run.** See "The winner" below.

The worked example is the dynamic search, `src/vibesys/orchestration/dynamic/search/`: the root
in `_search.py`, one flow per workstream, the roles in `_roles.py`, and the public surface in
`api.py`. To add a search, copy that layout (a `search/` package with `api.py`, roles beside the
root, a slim config model, prompt contexts as typed values), register it with `@search`, add its
plugin as described in [orchestration-runtime.md](orchestration-runtime.md), and test it with
`FlowRun` stories. `tests/architecture/test_search_packages.py` checks the package rules.

## The rule

There are two kinds of code.

- **Flow code** is an `async def` whose first argument is `ctx: Flow`. It may call `ctx`
  operations, other flow functions and pure functions, and use locals, loops, `match`, `try` and
  the `async with` constructs below. Nothing else: no `asyncio`, clock, randomness, threads, files,
  sockets or processes. Everything outside the flow reaches it through `ctx`.
- **Pure code** is ordinary Python that computes values (prompts, plan validation, settlement
  policy). No I/O, clock or randomness; tested with property tests.

Enforced by `tests/architecture/test_flow_code_is_restricted.py` (imports), lint (no blind
`except`), and the runtime (awaiting anything that is not a `ctx` operation raises at once).
A capability a flow lacks is added here, to the framework, and reviewed; never hand-rolled in a
search.

## `ctx`

`ctx` groups what a flow can do. Control flow is on `ctx` itself; everything that touches the
outside world is an aspect.

| Operation | What it does |
|---|---|
| `await ctx.first(*sources)` | The next value from whichever source is ready first |
| `async with ctx.group(limit=n) as g` | A scope for concurrent child flows |
| `await ctx.retry(attempt, policy)` | Repeat an attempt under a retry policy |
| `ctx.timer(at)` | A source that fires at a run-clock time |
| `ctx.stopped`, `ctx.stop_requested` | The operator's stop, as a source and as a flag |
| `ctx.checkpoint(snapshot)` | Where a code change may resume this flow |
| `ctx.choose(kept_or_INPUT, label=...)` | Name the current winner, and label the span that adopts it |
| `ctx.fall_back_to_input(label=...)` | Name the input the winner unless a candidate is already chosen |
| `ctx.publish(record)` | Make a typed record of the flow's progress visible outside |
| `ctx.label(kind, title=..., links=..., attrs=..., verdict=..., reason=...)`, `ctx.span` | Label this flow's span; the span's id, for another span's link |
| `ctx.facts`, `ctx.task`, `ctx.capabilities`, `ctx.clock.now()` | The run's facts and view (journaled reads); `ctx.task.run_id` keeps two runs' shared writes apart |
| `ctx.agents.session(role, ...)` | A conversation with an agent; `await session.turn(prompt, within=s)` |
| `ctx.workspaces.open(...)` | A workspace as a resource; `keep`, `verify`, `kept` |
| `ctx.evaluation.measure(...)` | Measure a revision; `readings`, `taken`, `last_evaluated` |
| `ctx.owners.perform(request, within=s)` | Ask an owner of an `owned=` operation (an issue board, say) |
| `async with my_resource(ctx, ...) as r` | Acquire and always release (`@resource`) |

A flow has no way to start a raw request, finish the run, shield itself, or read an arbitrary
slice of the run's view: those are core's own mechanics, behind the aspects.

### Agents

```python
session = ctx.agents.session(IMPLEMENTER, workspace=ws, revision=ws.base)
match await session.turn(ImplementerPrompt(...), within=config.implementer_turn_seconds):
    case TurnFinished(reply=ImplementerResult() as reply, checkpoint=checkpoint): ...
    case Failed(reason=FailureReason.INVALID_REPLY): ...
    case Failed() as failed: ...
```

A `Role` is the single declaration of how an agent is asked: name, prompt template, reply model
(a `RevisionReply` holds for core's checkpoint), workspace mode, access, charge, `turn_time`,
`corrections`, `lost_turns`, `lost_turn_backoff`, `correctable`, `render_time`,
the `Exchanges` templates for corrections and resumes, and whether it may wait on its own
evaluations. It is frozen and rejects unknown keys; core caps `corrections`, `lost_turns` and the
times (`MAX_*`). Only `model` is overridden per run (`SearchDef.with_models`). A session is one
conversation; the same session object continues it, a new session starts fresh.

`session.turn` returns **one** `TurnFinished` (the reply, already valid against the role's
model) or **one** `Failed`. In between core runs the turn: it renders the prompt from the
namespace's template, runs the provider turn, validates the reply and asks the agent to correct
an invalid one (up to `role.retry.corrections`), asks again after a lost turn with a backoff (up
to `role.lost_turns`), suspends a turn that waits for evaluations and resumes it when core
authorizes, and holds a write turn's reply until core recorded its checkpoint. A turn lost while it continued a wait is never asked again (core authorizes each resume
once), so it fails with `LOST`. A role with
`corrections=0` and `lost_turns=0` gets `Failed(INVALID_REPLY)` or `Failed(LOST)` at once and asks again itself. A
prompt that is a `Correction` makes the turn a correction of the earlier turn it names.

### Workspaces

```python
async with ctx.workspaces.open(attempt=a, item=i, plan=plan, budget=budget) as ws:
    if isinstance(ws, NotOpened):
        return Done.unopened(ws.failed)
    revision = await work(ctx, ws)
    ctx.workspaces.keep(ws, revision, Assessment(outcome="succeeded", eligible=True))
```

`open` is a resource: failures are values (`NotOpened(failed)`), and an opened workspace is
**always settled** on the way out. If the body kept a candidate (`keep`), the settlement asserts
it; if the body ended normally and kept nothing, the workspace is discarded as succeeded; if it
was stopped, as cancelled; if it raised, as failed (the exception propagates). If core refuses
what `keep` asserted, one plain `failed` settlement follows. `ws.kept` and `ctx.workspaces.kept()`
give the `Kept` candidates the settlements retained. `await ctx.workspaces.verify(revision,
within=s)` checks that a revision still reproduces its retained content, and `await
ctx.workspaces.export_patch(revision, within=s)` returns the canonical patch of a retained
revision against the input (the search declares the `export_patch` operation).

A role that writes (`WRITE_CANDIDATE`) works in its workspace in the mode core admitted it
(`ws.mode`: isolated, or the run's own root on a host without candidate sandboxes); a read-only
role keeps the mode its `Role` declares.

### Evaluation

`await ctx.evaluation.measure(plan, workspace=ws, within=s)` returns the `MeasurementResult` or
the `Failed`; `readings(refs, ...)` asks the evidence owner what it decoded; `taken(ws,
revision)` lists the trusted evaluations the agents took of a revision themselves, in the order
they happened; `last_evaluated(ws)` names the revision of the newest of them, which is where the
agents' own measuring left off.

### Sources and `first`

A **source** is anything a flow can wait on for a value:

| Source | Its values |
|---|---|
| `Task[T]` (from `group.start`) | the task's result, once |
| `Group[T]` | each finished task's result (or `TaskStopped(key)`), once each, in finishing order |
| `ctx.timer(at)` | `TimeUp(at)`, once |
| `ctx.stopped` | `StopRequested`, once |
| `ctx.latch(key)` | the value some flow `set` on the run-wide latch `key` (once per run), once per handle |
| `ctx.channel(key)` | each value any flow `send`s on the run-wide channel `key`, in the order sent, once; one flow reads it |

**Every source yields each value once.** `await source` waits for its next value;
`await ctx.first(a, b, c)` waits for the next value of whichever is ready first, and returns it
(ties go to the earliest source in the argument list). Values carry their own types, so the
caller matches on them:

```python
match await ctx.first(planning, workstreams, ctx.stopped):
    case PlanOutcome() as outcome: ...
    case WorkstreamDone() as done: ...
    case TaskStopped(key=key): ...
    case StopRequested(): ...
```

What a flow is waiting for is therefore always data: the runtime can say "ws-3 waits for
implementer.2 or the stop" in logs and tests. A flow waiting only on sources that can never yield
again (an empty group, a consumed stop, nothing pending) is a bug, raised as `Deadlock` with the
flow and its sources named. Concurrent agent turns or measurements are tasks in a group.

A **latch** carries a fact one flow learns to the flows beside it that need it, without passing
it through their arguments (a registered flow takes only its snapshot). Declare its key once,
typed by the value (`INPUT_MEASURED = LatchKey[Baseline]("input-measured")`); the flow that
learns the fact calls `ctx.latch(INPUT_MEASURED).set(value)`, once (a second `set` is a
`ContractError`), and a waiter calls `await ctx.latch(INPUT_MEASURED)`, which returns at once
when it is already set. A key is its declaration: a second key with a name already in use is a
`ContractError`, since the two could hold different types under one name.

Latches belong to one journal and are not journaled. Replay runs the flow code again, so it sets
them again at the same point. A restart after a code change starts a fresh journal from the flows'
snapshots, and every latch starts empty: the flow that sets one must set it again, from its
snapshot if it learned the value before the restart (`test_flows_restart.py` plays both orders).
A latch that is never set is not a `Deadlock`; the waiter waits for its stop.

A fact learned once per item (a verdict for each of many tasks) is one declaration with a latch
per item: `ctx.latch(VERDICT.of(task_key))`. Every item key belongs to its declaration, so the
flow that sets it and the flow that waits for it share the declared key, not an object.

A **channel** carries facts the other way: many flows report, as they reach them, to the one flow
that keeps their record (each workstream telling the root it waits for an evaluation slot).
Declare its key once (`REPORTS = ChannelKey[Report]("reports")`); any flow calls
`ctx.channel(REPORTS).send(value)`, and the reader waits on `ctx.channel(REPORTS)` in its
`first`. Values are read in the order they were sent, each once. **A channel has one reader**:
the first flow that waits on it reads it for the run, and a second flow that waits on it is a
`ContractError`, since two readers would split its values by scheduling order. A flow that waits
on an empty channel while no other flow is live waits for a value nobody can send, so the channel
counts as unable to yield again toward a `Deadlock`; while another flow lives, it may still send.

Like a latch, a channel is not journaled: replay sends again at the same points, and a restart
after a code change starts it empty. **So never record a checkpoint between a send and the
reader's receipt of what it implies.** A sender that checkpoints after its send resumes past it
on a restart, so the reader, whose channel starts empty, never hears it; a reader that
checkpoints after it read the value but before it folded it in loses the fact the same way. Send
after the last checkpoint that a restart resumes from, as the dynamic search's workstreams do
(each send of theirs follows the checkpoint it is redone from: a workstream that resumes past the
input's measurement holds the baseline in its own snapshot, and the root sets `INPUT_MEASURED`
again from its own). Unenforced: a
checkpoint and a send are ordinary calls in flow code, and which pairs matter depends on what the
reader does with the value; `test_flows.py` plays every channel story restarted after each
step, which catches a lost send in the flows it covers.

### Groups

```python
async with ctx.group(limit=config.max_in_flight) as workstreams:
    workstreams.start(start.key, workstream, start)     # returns Task[WorkstreamDone]
    workstreams.free                                    # slots left under the limit
    async for done in workstreams: ...                  # every result, as they finish
```

A child can never outlive its group: leaving the `async with` waits for every task still
running. `start` on a full group raises; read `free` first.

**Stops.** The operator's stop (and the run deadline, which the host delivers as a stop) does not
end the run by itself; it tells the flows. Every task in a group with `stops=True` (the default)
receives `Stopped` at its current `await`, once. A task that lets `Stopped` escape ends, and its
group yields `TaskStopped(key)` in its place; cleanup belongs in a resource, so most flows never
mention `Stopped`. A group with `stops=False` is not interrupted. A stop is raised before
anything is issued: at an unshielded aspect call or `first`, a pending stop raises `Stopped`
first. The root flow is never interrupted; it sees `ctx.stopped` and `ctx.stop_requested`, winds
the rest down and returns.

### Resources

A resource is written like `contextlib.asynccontextmanager`:

```python
@resource
async def checkout(ctx: Flow, w: WorkstreamStart) -> AsyncGenerator[Checkout]:
    opened = await acquire(ctx, w)
    try:
        yield opened
    finally:
        await release(ctx, opened)
```

Entering a resource is a checkpoint: a stop already pending for the task is raised at
`async with`, before the acquire runs. The code before `yield` and after it is shielded. Whatever
way the body ends, the release runs. `ctx.workspaces.open` is a resource.

### Retry

```python
class Again[R](Value): reason: R             # "try again, because R"
class Tries[R](Value): number: int; reasons: tuple[R, ...]
class GaveUp[R](Value): reasons: tuple[R, ...]
class RetryPolicy(Value): max_tries: int; backoff: Backoff | None = None

result = await ctx.retry(lambda tries: implementer_turn(ctx, w, a, tries.reasons), policy)
```

The attempt returns a value (done) or `Again(reason)` (try again). The policy bounds the tries
and, with a backoff, waits on the run clock between them. `ctx.retry` returns the value or
`GaveUp(reasons)`.

### Publishing progress

`ctx.publish(record)` replaces the flow's published record (a pydantic model): what status views,
projections and tests read about a run in flight. It is kept in the state beside the journal.

### The run trace

The runtime records what the run's work did as **spans**, without the search doing anything: one
span for each flow task (the root and every task a group starts) and one for each operation a flow
awaits (an agent turn and its render, a workspace's start and settlement, a verification, a
measurement and its interpretation, an owner operation, the adoption). Each is recorded at the
point where the runtime already handles that transition, so a new operation kind or flow
construct gets its span from the same few points.

| Field | What it holds |
|---|---|
| `id` | `flow:<path>#<n>` for a task (`n` counts the tasks started at that path, since a key can be started again after its task ended; the root is `flow:#1`), `op:<decision id>` for an operation |
| `parent` | the span of the flow that started the task or issued the operation (None for the root) |
| `type`, `name` | `flow` with the flow's name, or `operation` with the operation's registered kind (or, for core's own requests, the id root core issued it under: the role's name for a turn, `measure`, `start`, `settle`, `adopt`...) |
| `status` | `running` (an operation in flight, or a flow waiting on its own work: an operation, a turn, a task or a group), `waiting` (a flow waiting only on time, a latch or the stop), `pending` (a flow a code change restarts, until its parent starts its key again), `done`, `failed`, `stopped` |
| `waiting_on` | one entry per source of the flow's current wait: `operation` (ref: the operation's span, or a turn's signal), `slot` (a task's span, `group`, or a latch), `time` (the timer), `stop`; `since` is when that wait began |
| `attempts` | each try of `ctx.retry` in the flow: `n` of `of`, its times, and `done`, `again` (with the reason as text) or `gave_up` |
| `started_at`, `ended_at` | run-clock times |
| `failure` | `outcome` when a `failed` span ended with a failure value (an operation answered `Failed`, a task returned `Failed` or `GaveUp`, the run finished as a failure) and `interruption` when a `stopped` span was interrupted (the stop landed, the flow abandoned the operation, a code change dropped it, the run ended around it) |
| `stop` | when the operator's stop reached this flow, and whether it `landed` (raised `Stopped` in it) |
| `invocation` | for an agent turn, the invocation it ran as: how the runtime names the turn's span on the agent's own records (its usage) |
| `label` | the search's label of the span (see "Span labels"), or None |

**Determinism.** Every journal step records its run-clock time (`Step.at`, the host's `now_at`
when it delivered the event); every span transition the step makes carries that time, and replay
reads it back from the step. It is an input like the event, so it is not in the digest. Flow code
is deterministic, so replaying a journal yields the same spans, ids and times. The spans are kept
beside the live flows in the persisted state; a restart after a code change continues them: a
registered flow resumes in its own span (pending until its parent starts its key again), and an
operation in flight or a task that is not a registered flow ends `stopped` (a fresh start of that
key gets the next `#n`). When the root issues the run's `Finish`, every span still open ends, and
the root ends `done`, `failed` or `stopped` by the proposed result, so a finished run has no
running or waiting span. A defect crashes the step that raised it, which is never persisted, so a
trace holds no defect.

The shell reads the spans as `FlowDriver.trace(state)`, a frozen `RunTrace` of `Span`s (exported
from `vs_core.api`); `FlowState` itself stays private. Clients get the trace as span events and
snapshot spans: see "Run trace events" in [wire-protocol.md](wire-protocol.md). `test_flows_trace.py` and the sweep in
`test_flows_properties.py` check the properties, live and with a restart after every step.

### Span labels

Runtime spans say what ran; the search says what it means. It declares the kinds of span it
labels at registration, as it declares roles:

```python
WORKSTREAM = SpanKind(
    "workstream", attrs=WorkstreamAttrs, verdicts=("passed", "failed"), links=("builds_on",)
)
AGENT_TURN = SpanKind("agent_turn", attrs=AgentTurnAttrs)

@search("dynamic", config=DynamicConfig, roles=ROLES, spans=(WORKSTREAM, AGENT_TURN))
```

A kind's `attrs` is a pydantic model that forbids unknown keys (refused at declaration
otherwise); `verdicts` and `links` are the verdict values and link relations it allows. Two kinds
with one name are refused at `@search`.

```python
ctx.label(WORKSTREAM, title="ws-3", links=(SpanLink(rel="builds_on", to=base.span),))
...
ctx.label(WORKSTREAM, verdict="rejected", reason="slower")    # title and links stay
await session.turn(prompt, within=s, label=Label(AGENT_TURN, attrs={"role": "implementer"}))
```

- `ctx.label(kind, **fields)` labels the current flow's span. The fields it names (`title`,
  `links`, `attrs`, `verdict`, `reason`) replace the earlier ones; the rest stay. A span keeps
  the kind of its first label. `ctx.label(kind, span=s, ...)` labels a span this flow started
  instead: a task of its group (`task.span`), or an operation it issued. `ctx.operation_span` is
  the span of the operation the flow issued last, so a flow can label a measurement with what
  only its answer says (the value, the verdict). A span the flow did not start is refused
  (`label.span`).
- `label=Label(kind, ...)` on `session.turn`, `ctx.evaluation.measure`, `ctx.workspaces.verify`
  and `ctx.owners.perform` labels the operation's span when it is issued (for a turn, each
  provider turn of the ask; not its render).
- A link names a span by its `SpanId`: `ctx.span` for the current flow, `task.span` for a task a
  group started. A link must name a span the run has already recorded.
- `verdict` is the search's judgement, kept apart from `status`: a workstream can be `done` and
  `rejected`.
- Anything undeclared is a `ContractError` naming it: a kind (`label.kind`), an attribute
  (`label.<kind>.attrs.<key>`), a verdict (`label.<kind>.verdict`), a relation
  (`label.<kind>.links.<rel>`), or a link to a span the run never recorded
  (`label.<kind>.links.<span id>`). Like any defect it crashes the run.

The span holds the label as a `SpanLabel` (the kind's name, the fields, and `attrs_json`, the
attributes as JSON). A label is an effect of flow code, like `ctx.publish`: replay runs the same
calls at the same points, so labels replay identically. Labels are not in the digest, so a code
change that only relabels does not restart the flows (replay relabels the spans with the new
code); a restart after a code change keeps the labels the spans had.

### Checkpoints and code changes

A flow registered with `@flow(snapshot=S)` takes its snapshot as its argument and records newer
ones with `ctx.checkpoint(s)` (synchronous). The flow's name defaults to the function's name.
If a run resumes on changed code (the journal no longer replays), every live flow restarts from
its latest snapshot with the new code. **One rule for every flow, the root included: a flow
resumes only from a checkpoint it recorded; with none, the restart raises `FlowCodeChanged`.**
Silently restarting a flow from its start arguments would redo work that was paid for (agent
turns, measurements). A flow that is safe to restart from its arguments says so by calling
`ctx.checkpoint(args)` as its first act:

```python
@flow(snapshot=Job)
async def worker(ctx: Flow, job: Job) -> Done:
    ctx.checkpoint(job)            # restartable from its arguments
    ...
    ctx.checkpoint(job.after(step))  # later, a newer resume point
```

The root may declare its own snapshot (`search(..., snapshot=S)`); it then takes `config | S`
and resumes from its latest `ctx.checkpoint(S(...))`, holding what it had learned (the dynamic
search's `DynamicResume`: its record and the input's measurement once it landed). A root without
one resumes from its config. A snapshot type may be a union of versions (`snapshot=V1 | V2`):
give each model a `version` literal, and a checkpoint that decodes as none of them raises
`FlowCodeChanged` as any other unresumable change does. The restart resolves each flow
through `group.start(key, fn, ...)`, which resumes the flow that had that key from its snapshot if
the new code starts a flow of the same name there (otherwise `FlowCodeChanged`). Work since the
last checkpoint is redone, and the results of requests in flight at the change are dropped when
they arrive. A task that is not a registered flow is part of its parent's work: it has no
snapshot, so the restart drops it, and when the parent, restarted from its own checkpoint, starts
that key again, the task starts afresh.

Replay notices a code change when the new code issues different decisions, makes a different
number of reads, or reads a recorded value as a type it no longer decodes as (the old code read
the clock where the new code reads `ctx.task`). Each of these restarts the flows as above.

**Resources do not leak across a restart, and work in them is not discarded silently.** A
workspace a flow held at its latest checkpoint is carried across the restart: the resumed flow
takes it back with `ctx.workspaces.reopen(attempt)` (a `Workspace`, or `NotReopened` when the
attempt is not one this flow carried), and keeps working in it. Every other held workspace is
settled as part of the restart (decision ids `restart-settle.<n>`), and the reason is recorded in
`FlowState.restart_settled` (`RestartSettleReason`): opened after the checkpoint, flow not
resumable, flow not restarted, not reopened, already settling, or unclaimed. The settlement is a
plain `cancelled` discard unless the flow said otherwise with `ctx.workspaces.salvage(ws,
revision, assessment)`, which names how the workspace settles if the restart cannot give it back
(the dynamic search salvages its latest candidate as unmeasured work in progress).

Requests in flight at the change are not asked twice. A resumed flow that asks again for a
measurement of the same plan, a settlement of the same workspace, or the opening of the same
workspace is attached to the request already in flight (`LiveFlows.in_flight`) and gets its
answer. An opening or a settlement core answers before the resumed flow asks again is kept for
the flow at that path (`InFlight.answer`), so the workspace still opens once; a measurement
answered first is dropped, and measured again if asked. A workspace whose settlement was in flight
is left to it (`ws.settling` on reopen, reason `settling`); a workspace that opens with no flow
left to claim it, or whose kept opening its flow ends without taking, is settled as `unclaimed`.

## The winner

A flow never finishes the run. It names the winner as it goes:

```python
ctx.choose(kept)        # a Kept candidate a closed workspace retained
ctx.choose(INPUT)       # nothing beat the input
```

`ctx.choose` may be called at any time, from any flow; the last choice stands. It takes the
`Label` of the span that adopts it (`ctx.choose(kept, adoption_label(...))`): the adoption is one
operation (the `ProposeWinner`), the framework labels its span with the last choice's label when
it is issued, and sets the verdict `ADOPTED` or `NOT_ADOPTED` (with the typed reason) once core has
answered, so the kind must declare both verdicts. A choice without a label adopts unlabelled,
and so does a run that chose nothing (the input stands): a search that labels its adoption starts
with `ctx.fall_back_to_input(label)`, and calls it again as it learns more about the input; it
never replaces a candidate already chosen. When the root
returns, or the operator stops the run, or the deadline passes, the framework ends the run by one
path:

1. If the root returned `NoWinner(reason)`: the run fails with reason `no-winner: <reason>`
   (cancelled if it was stopped), and nothing is adopted. `NoWinner(reason, keep_choice=True)`
   fails the run the same way but adopts the last choice first and carries it as the selection
   (a search that ran out of budget after building something worth keeping).
2. Otherwise core adopts the last choice (nothing chosen: the input, as it stands).
3. If adoption fails, the run fails with `adoption-failed: <typed reason>`.
4. If it succeeds, the run succeeds with the selection; a run that was stopped or timed out is
   `cancelled` (reasons `stopped: ...` / `deadline: ...`) and still carries the selection.
5. The framework finishes the run. If core refuses the result, one plain failure is proposed.

The host no longer commits a "cancelled" result at a stop or at the deadline: both are controls
the flows see, and the same single path ends the run. Closing out (settling a workspace,
adopting) is accepted after the deadline.

## Failures, three kinds

1. **Outcomes** are values: `Failed`, `GaveUp`, `TaskStopped`, `NotOpened`, a typed result.
   Match on them.
2. **Interruption** is only `Stopped`, delivered at an unshielded `await`.
3. **Defects** are everything else. A flow may raise its own typed exception to leave a deep call
   early, but it must catch it itself; any exception that escapes a task, other than `Stopped`,
   crashes the run loudly. Flow code never uses a blind `except`.

## Testing a flow

Two levels, both through public APIs and fakes.

`vs_core.api.testing.FlowRun` is a fake host that plays one flow search with scripted outcomes,
no real time. It answers prompt renders itself and adopts a proposed winner at once (unless told
not to), and the test answers the rest:

```python
def test_a_lost_turn_is_asked_again(begin: Begin) -> None:
    run = begin(root)
    run.lose(run.expect_turn("hello"))
    run.advance_to(1.0)                         # the backoff
    run.reply(run.expect_turn("hello"), Plan(n=3))
    assert run.finished is not None
```

- `begin` runs every story twice: live, and with a save-and-restart after every step.
- `RestartPolicy(steps=..., changes=(changed, original))` changes the code at those steps instead
  of only saving: `code_changed(definition)` is the same search under code that no longer
  replays, so every flow restarts from its checkpoint. Change at sampled steps, not every step:
  a change at every step restarts the run before it can progress.
- `run.waiting()` says what each flow waits for (its sources), so a test can assert it.
- `run.trace()` is the run trace so far (see "The run trace").
- `run.timeline()` prints the run as a timeline (`worker.1:Plan stop t=30`); property tests print
  their shrunk counterexamples this way.

For the contract between a search and the real core (turns, corrections, lost turns, suspension,
workspaces, adoption) a toy search runs through the real core with scripted executors
(`vs_core.testing.drive`); see `libs/vs-core/tests/toy_agent_search.py`.

## Not in the vocabulary, on purpose

- A way to start a raw request, finish the run, or shield a region: those are core's.
- Scope-wide deadlines (Trio's `move_on_after`): a bound belongs to the call that launches the
  work.
- Callbacks (`then=`), predicates (`until(lambda)`) and operator chains: waiting is on sources,
  and sequencing is `await`.
- A crash-loop budget: a run whose replay keeps failing is stopped by the shell, which counts
  restarts.

## Implementation notes

- Operation results (`OperationFinished`) are journaled as JSON and rebuilt through the
  `OperationRegistry` the shell gives the driver; every other event is journaled as itself.
- The persisted `FlowState` is core's private mechanism; the shell runs a search through the
  private driver (`SearchDef.driver(config, registry)`).
- Reads (`ctx.facts`-derived views, `ctx.task`, `ctx.capabilities`, `ctx.clock.now()`) are
  journaled, so they replay to the same value.
- `Group.free` is a large constant when the group has no limit. `ctx.stopped` yields the
  `StopRequested` event itself. A stopped `Task` yields `TaskStopped(key)`.
- Awaiting inside a `finally` while the run itself is being discarded is not supported; use
  `except Stopped` (or `@resource`) for cleanup that awaits.
- A suspended turn's waits are bounded by the run deadline, so a turn that never resumes cannot
  hold the run.
