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
| `ctx.choose(kept_or_INPUT)` | Name the current winner |
| `ctx.publish(record)` | Make a typed record of the flow's progress visible outside |
| `ctx.facts`, `ctx.task`, `ctx.capabilities`, `ctx.clock.now()` | The run's facts and view (journaled reads); `ctx.task.run_id` keeps two runs' shared writes apart |
| `ctx.agents.session(role, ...)` | A conversation with an agent; `await session.turn(prompt, within=s)` |
| `ctx.workspaces.open(...)` | A workspace as a resource; `keep`, `verify`, `kept` |
| `ctx.evaluation.measure(...)` | Measure a revision; `readings`, `taken` |
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
they happened.

### Sources and `first`

A **source** is anything a flow can wait on for a value:

| Source | Its values |
|---|---|
| `Task[T]` (from `group.start`) | the task's result, once |
| `Group[T]` | each finished task's result (or `TaskStopped(key)`), once each, in finishing order |
| `ctx.timer(at)` | `TimeUp(at)`, once |
| `ctx.stopped` | `StopRequested`, once |

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

The root's snapshot type is the config (`ctx.checkpoint(config)`). The restart resolves each flow
through `group.start(key, fn, ...)`, which resumes the flow that had that key from its snapshot if
the new code starts a flow of the same name there (otherwise `FlowCodeChanged`). Work since the
last checkpoint is redone, and the results of requests in flight at the change are dropped when
they arrive. A task that is not a registered flow is part of its parent's work: it has no
snapshot, so the restart drops it, and when the parent, restarted from its own checkpoint, starts
that key again, the task starts afresh.

Replay notices a code change when the new code issues different decisions, makes a different
number of reads, or reads a recorded value as a type it no longer decodes as (the old code read
the clock where the new code reads `ctx.task`). Each of these restarts the flows as above.

**Resources do not leak across a restart.** A workspace a flow opened after its checkpoint is
settled as part of the restart: the restart's first decisions withdraw each such attempt plainly
as `cancelled` (decision ids `restart-settle.<n>`), and the restarted flow opens its workspaces
again.

## The winner

A flow never finishes the run. It names the winner as it goes:

```python
ctx.choose(kept)        # a Kept candidate a closed workspace retained
ctx.choose(INPUT)       # nothing beat the input
```

`ctx.choose` may be called at any time, from any flow; the last choice stands. When the root
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
- `run.waiting()` says what each flow waits for (its sources), so a test can assert it.
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
