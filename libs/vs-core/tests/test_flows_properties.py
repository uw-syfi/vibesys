"""Crash-at-every-step sweep: a restart after every step tells the same story as a live run.

Toy flows are generated from small programs (asks, timers, races between a turn and a timer
or the stop, retries, resources, groups of tasks with their own programs, and what searches use:
agent sessions asked several times, workspaces that are kept, verified and measured, and winner
choices), and so are the event orders (answer an open wait with a reply or a failure, move the
clock, stop). The same orders drive two runs in lockstep: one live, one that writes its state to
JSON and replays it in a new search after every step. They must agree on every decision, every
state, what every flow waits for and the run trace's spans, ids and times; the trace stays well
formed at every step. A counterexample is printed as a timeline.

Once the generated events are spent the host stops the run and answers everything left. Then,
besides the two runs agreeing: every workspace the host opened was settled exactly once, a session
kept one conversation across its turns, and the winner the run adopted is the last one the live
flow chose (the input when it chose none), and no span is left running or waiting.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from hypothesis import event, example, given, note, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.testing import Prompt
from vs_sim.api.testing import examples

from .flow_support import (
    FACTS,
    WORKER,
    Config,
    Plan,
    answer_of,
    ask,
    assert_trace_ended,
    assert_trace_well_formed,
    open_workspace,
    start,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from vs_core.api.testing import Asked, FlowRun


class Seen(core.Value):
    """What the toy flow publishes at its end: what its program saw."""

    text: str


@dataclass(frozen=True)
class Ask:
    label: str


@dataclass(frozen=True)
class Sleep:
    until: float


@dataclass(frozen=True)
class Race:
    """A request against a timer (or, with ``until`` None, against the stop)."""

    label: str
    until: float | None


@dataclass(frozen=True)
class Retry:
    tries: int
    backoff: bool


@dataclass(frozen=True)
class Guarded:
    """A resource around one ask: setup, body, teardown."""

    label: str


@dataclass(frozen=True)
class Session:
    """One agent session asked ``turns`` times: the same session object continues the talk."""

    turns: int


@dataclass(frozen=True)
class Choose:
    """Name a kept candidate the winner (``None``: the input)."""

    name: str | None


@dataclass(frozen=True)
class Work:
    """A workspace around some work; afterwards the flow may choose what it retained."""

    asks: bool
    measure: bool
    verify: bool
    keep: Literal["nothing", "candidate", "bare"]
    choose: bool


@dataclass(frozen=True)
class Scope:
    """A group of tasks, each running its own program; the flow drains their results."""

    tasks: tuple[tuple[Op, ...], ...]
    stops: bool


type Op = Ask | Sleep | Race | Retry | Guarded | Session | Choose | Work | Scope

_SESSION = st.builds(Session, turns=st.integers(1, 3))
_CHOOSE = st.builds(Choose, name=st.none() | st.sampled_from(["a", "b", "c"]))
_WORK = st.builds(
    Work,
    asks=st.booleans(),
    measure=st.booleans(),
    verify=st.booleans(),
    keep=st.sampled_from(["nothing", "candidate", "bare"]),
    choose=st.booleans(),
)
_LEAVES = st.one_of(
    st.builds(Ask, label=st.sampled_from(["x", "y"])),
    st.builds(Sleep, until=st.integers(1, 30).map(float)),
    st.builds(
        Race, label=st.sampled_from(["r", "s"]), until=st.none() | st.integers(1, 30).map(float)
    ),
    st.builds(Retry, tries=st.integers(1, 3), backoff=st.booleans()),
    st.builds(Guarded, label=st.just("g")),
    _SESSION,
    _CHOOSE,
    _WORK,
)
_SEARCH_LEAVES = st.one_of(
    st.builds(Ask, label=st.just("x")),
    st.builds(Guarded, label=st.just("g")),
    _SESSION,
    _CHOOSE,
    _WORK,
    _WORK,
)
"""The two vocabularies: everything, and mostly what searches do (sessions, workspaces, choices)."""


def _programs(leaves: st.SearchStrategy[Op]) -> st.SearchStrategy[tuple[Op, ...]]:
    def bodies(inner: st.SearchStrategy[tuple[Op, ...]]) -> st.SearchStrategy[tuple[Op, ...]]:
        scope = st.builds(Scope, tasks=st.lists(inner, max_size=2).map(tuple), stops=st.booleans())
        return st.lists(st.one_of(leaves, scope), min_size=1, max_size=4).map(tuple)

    return st.recursive(st.lists(leaves, min_size=1, max_size=4).map(tuple), bodies, max_leaves=10)


PROGRAMS = st.one_of(_programs(_LEAVES), _programs(_SEARCH_LEAVES))


@core.resource
async def guard(ctx: core.Flow, label: str) -> AsyncGenerator[str]:
    await ask(ctx, f"{label}-open")
    try:
        yield label
    except core.Stopped:
        await ask(ctx, f"{label}-close")
        raise
    await ask(ctx, f"{label}-close")


def revision_of(name: str) -> core.RevisionRef:
    """The revision a toy flow's work named ``name`` makes."""
    return core.RevisionRef(revision_id=core.RevisionId(root=name), digest=f"{name}-digest")


def kept_named(name: str) -> core.Kept:
    """A candidate some closed workspace retained, as ``Choose`` names it."""
    return core.Kept(
        settlement=core.Settlement(
            settlement_id=core.SettlementId(root=name),
            attempt=core.AttemptRef(attempt_id=core.AttemptId(root=name), generation=0),
            candidate=revision_of(name),
            assessments=(),
            eligible=True,
            retention="candidate",
            outcome="succeeded",
        )
    )


def selection_of(kept: core.Kept | None) -> core.Selection:
    """What the run adopts when ``kept`` is the last choice (``None``: the input)."""
    if kept is None:
        return core.TrustedBaseline(revision=FACTS.baseline)
    return core.RetainedCandidate(
        settlement_id=kept.settlement.settlement_id, revision=kept.revision
    )


def measurement_of(revision: core.RevisionRef) -> core.MeasurementPlan:
    """A one-stage measurement of ``revision``."""
    return core.MeasurementPlan(
        purpose="local-validation",
        candidate=revision,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(core.MeasurementStage(stage_id="measure", execution_budget=10.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        accuracy_gated=(),
    )


class Interpreter:
    """The toy flow every generated program is an instance of; it logs each choice it makes."""

    def __init__(self, chosen: list[core.Kept | None]) -> None:
        """Log choices to ``chosen``."""
        self.chosen = chosen

    def choose(self, ctx: core.Flow, kept: core.Kept | None) -> None:
        """Name ``kept`` the winner (``None``: the input) and log it."""
        self.chosen.append(kept)
        ctx.choose(core.INPUT if kept is None else kept)

    async def work(self, ctx: core.Flow, op: Work, name: str) -> str:
        """A workspace around the work ``op`` says, kept as it says."""
        seen: list[str] = []
        async with open_workspace(ctx, name) as ws:
            if isinstance(ws, core.NotOpened):
                return "not-opened"
            revision = revision_of(name)
            if op.asks:
                seen.append(type(await ask(ctx, f"{name}-in")).__name__)
            if op.measure:
                plan = measurement_of(revision)
                found = await ctx.evaluation.measure(plan, workspace=ws, within=10.0)
                seen.append(type(found).__name__)
            if op.verify:
                seen.append(type(await ctx.workspaces.verify(revision, within=10.0)).__name__)
            if op.keep == "candidate":
                kept = core.Assessment(outcome="succeeded", eligible=True)
                ctx.workspaces.keep(ws, revision, kept)
            elif op.keep == "bare":
                ctx.workspaces.keep(
                    ws, None, core.Assessment(outcome="failed", retention="discard")
                )
        if op.choose and ws.kept is not None:
            self.choose(ctx, ws.kept)
        seen.append("kept" if ws.kept is not None else "nothing-kept")
        return ",".join(seen)

    @staticmethod
    async def race(ctx: core.Flow, label: str, until: float | None) -> str:
        """A request against a timer, or against the stop."""
        async with ctx.group(stops=False) as racing:
            pending = racing.start("race", _asked, label)
            other = ctx.stopped if until is None else ctx.timer(until)
            return type(await ctx.first(pending, other)).__name__

    @staticmethod
    async def session(ctx: core.Flow, op: Session, label: str) -> str:
        """One session asked ``op.turns`` times."""
        session = ctx.agents.session(WORKER)
        seen = [
            type(await session.turn(Prompt(text=f"{label}-{turn}"), within=10.0)).__name__
            for turn in range(op.turns)
        ]
        return ",".join(seen)

    async def scope(self, ctx: core.Flow, op: Scope, prefix: str) -> str:
        """A group of tasks, each running its program.

        ``prefix`` names the scope by its position, so the tasks of two scopes never share a
        workspace or a conversation name: core refuses a second workspace of one attempt, and
        the host's ledger is keyed by name.
        """
        async with ctx.group(stops=op.stops) as group:
            for number, task in enumerate(op.tasks):
                group.start(f"t{number}", self.run, task, f"{prefix}t{number}.")
            return ",".join([str(done) async for done in group])

    async def step(self, ctx: core.Flow, op: Op, prefix: str, index: int) -> str:
        """Run one op; what it saw."""
        seen = ""
        match op:
            case Ask(label=label):
                seen = type(await ask(ctx, label)).__name__
            case Sleep(until=until):
                await ctx.timer(until)
            case Race(label=label, until=until):
                seen = await self.race(ctx, label, until)
            case Retry(tries=tries, backoff=backoff):
                seen = str(await ctx.retry(_attempt(ctx), _policy(tries, backoff=backoff)))
            case Guarded(label=label):
                async with guard(ctx, label):
                    await ask(ctx, f"{label}-body")
            case Session():
                seen = await self.session(ctx, op, f"{prefix}s{index}")
            case Choose(name=name):
                self.choose(ctx, None if name is None else kept_named(name))
            case Work():
                seen = await self.work(ctx, op, f"{prefix}w{index}")
            case Scope():
                seen = await self.scope(ctx, op, f"{prefix}{index}")
        return seen

    async def run(self, ctx: core.Flow, body: tuple[Op, ...], prefix: str = "") -> str:
        """Run a program."""
        return ",".join([await self.step(ctx, op, prefix, i) for i, op in enumerate(body)])


def make_root(
    body: tuple[Op, ...], chosen: list[core.Kept | None]
) -> Callable[[core.Flow, Config], Coroutine[Any, Any, None]]:
    """The toy flow of a program; every choice it makes is appended to ``chosen``."""
    program = Interpreter(chosen)

    async def root(ctx: core.Flow, _config: Config) -> None:
        await ask(ctx, "start")
        ctx.publish(Seen(text=await program.run(ctx, body) or "empty"))

    return root


async def _asked(ctx: core.Flow, label: str) -> str:
    return type(await ask(ctx, label)).__name__


def _policy(tries: int, *, backoff: bool) -> core.RetryPolicy:
    return core.RetryPolicy(
        max_tries=tries, backoff=core.Backoff(first=2.0, cap=120.0) if backoff else None
    )


def _attempt(
    ctx: core.Flow,
) -> Callable[[core.Tries[str]], Coroutine[Any, Any, int | core.Again[str]]]:
    async def attempt(tries: core.Tries[str]) -> int | core.Again[str]:
        n = answer_of(await ask(ctx, "t"))
        return n if n > 0 else core.Again(reason=str(tries.number))

    return attempt


_MOVE = st.tuples(st.integers(0, 99), st.integers(1, 15), st.integers(0, 3))
"""One host move: which of the open possibilities, how far the clock goes, what a reply says."""


@dataclass
class Host:
    """What the host saw while it played the live run: the ledger the end-of-run claims read."""

    live: FlowRun[Config]
    opened: list[core.AttemptRef] = field(default_factory=list)
    settled: Counter[core.AttemptRef] = field(default_factory=Counter)
    refused: set[core.AttemptRef] = field(default_factory=set)
    sessions: dict[tuple[str, str], str] = field(default_factory=dict)

    def note_sessions(self) -> None:
        """Record the session of every open session turn: one session per ``Session`` op."""
        for asked in self.live.open:
            request = asked.request
            if asked.prompt is None or not isinstance(request, core.RequestTurn):
                continue
            conversation, _, turn = asked.prompt.text.rpartition("-")
            if not (turn.isdigit() and conversation.rpartition("s")[2].isdigit()):
                continue  # not a Session op's turn
            key = (asked.path, conversation)
            session = request.turn.session.session_id.root
            assert self.sessions.setdefault(key, session) == session, (
                f"{key} changed session: {self.sessions[key]} then {session}"
            )
        assert len(set(self.sessions.values())) == len(self.sessions)


@settings(max_examples=examples(pr=3, nightly=60), deadline=None)
@given(body=PROGRAMS, script=st.lists(_MOVE, max_size=16))
# The last choice stands: a first-choice-wins flow adopts the input here.
@example(
    body=(
        Choose(name=None),
        Work(asks=False, measure=False, verify=False, keep="candidate", choose=True),
    ),
    script=[],
)
# Two scopes whose tasks run the same program each open their own workspace and session.
@example(
    body=(
        Scope(
            tasks=(
                (
                    Session(turns=1),
                    Work(asks=False, measure=False, verify=False, keep="nothing", choose=False),
                ),
            ),
            stops=False,
        ),
        Scope(
            tasks=(
                (
                    Session(turns=1),
                    Work(asks=False, measure=False, verify=False, keep="nothing", choose=False),
                ),
            ),
            stops=False,
        ),
    ),
    script=[],
)
# A stopped task still settles its workspace: stop while the task is inside it.
@example(
    body=(
        Scope(
            tasks=((Work(asks=True, measure=False, verify=False, keep="nothing", choose=False),),),
            stops=True,
        ),
    ),
    script=[(2, 1, 0), (2, 1, 0), (1, 1, 0)],
)
def test_a_restart_after_every_step_equals_the_live_run(
    body: tuple[Op, ...], script: list[tuple[int, int, int]]
) -> None:
    live_choices: list[core.Kept | None] = []
    live = start(make_root(body, live_choices))
    crashing = start(make_root(body, []), restarting=True)
    host = Host(live)
    for move in script:
        if not _both(live, crashing, _action(move, host)):
            return
        note(live.timeline())
        host.note_sessions()
    if not (_both(live, crashing, lambda r: r.stop()) and _drain(live, crashing, host)):
        return  # a generated program may deadlock; both runs did
    note(live.timeline())
    event(f"workspaces opened: {min(len(host.opened), 3)}")
    event(f"choices made: {min(len(live_choices), 3)}")
    event(f"sessions: {min(len(host.sessions), 3)}")
    assert live.finished is not None, f"the run never finished: {live.waiting()}"
    assert crashing.finished == live.finished
    assert all(host.settled[ref] == 1 for ref in host.opened), host.settled
    assert set(host.settled) <= set(host.opened)
    result = live.finished.result
    assert result.selection == selection_of(live_choices[-1] if live_choices else None)
    assert_trace_ended(live.trace())


def _both(
    live: FlowRun[Config], crashing: FlowRun[Config], action: Callable[[FlowRun[Config]], None]
) -> bool:
    """Apply ``action`` to both runs; they must agree. False once a deadlock ends them."""
    outcomes = []
    for run in (live, crashing):
        try:
            action(run)
            outcomes.append("ok")
        except core.Deadlock as deadlock:  # a generated program may deadlock; both must
            outcomes.append(f"deadlock {deadlock.flow} {deadlock.sources}")
    note(live.timeline())
    assert outcomes[0] == outcomes[1]
    if outcomes[0] != "ok":
        return False
    assert live.decisions == crashing.decisions
    assert live.state == crashing.state
    assert live.open == crashing.open
    assert live.waiting() == crashing.waiting()
    assert live.trace() == crashing.trace()
    assert_trace_well_formed(live.trace(), live.view.now_at)
    return True


def _drain(live: FlowRun[Config], crashing: FlowRun[Config], host: Host) -> bool:
    """After the stop, answer whatever the flows still wait for, until the run finishes."""
    for _ in range(400):
        waits = [asked for asked in live.open if not isinstance(asked.request, core.Finish)]
        if waits:
            action = _reply(host, waits[0], 1)
        elif live.finished is None and live.sleeping_until:
            target = max(*live.sleeping_until, live.view.now_at)  # the run clock never goes back
            action = _advance(target)
        else:
            return True
        if not _both(live, crashing, action):
            return False
        host.note_sessions()
    message = f"the drain did not end: {live.timeline()}"
    raise AssertionError(message)


def _advance(target: float) -> Callable[[FlowRun[Config]], None]:
    return lambda run: run.advance_to(target)


def _action(move: tuple[int, int, int], host: Host) -> Callable[[FlowRun[Config]], None]:
    """Draw the next host action from what the live run has open."""
    run = host.live
    open_waits = [asked for asked in run.open if not isinstance(asked.request, core.Finish)]
    choices = ["advance", "stop"]  # the host never answers the run's own Finish
    choices += [f"reply{i}" for i, _ in enumerate(open_waits)]
    choices += [f"fail{i}" for i, asked in enumerate(open_waits) if _may_fail(host, asked)]
    pick, step, answer = move
    choice = choices[pick % len(choices)]
    if choice == "stop":
        return lambda r: r.stop()
    if choice == "advance":
        return _advance(run.view.now_at + step)
    asked = open_waits[int(choice.removeprefix("reply").removeprefix("fail"))]
    if choice.startswith("fail"):
        return _fail(host, asked)
    return _reply(host, asked, answer)


def _may_fail(host: Host, asked: Asked) -> bool:
    """The host refuses a settlement at most once per workspace, so every workspace settles."""
    request = asked.request
    return not (isinstance(request, core.Withdraw) and request.target in host.refused)


def _fail(host: Host, asked: Asked) -> Callable[[FlowRun[Config]], None]:
    request = asked.request
    if isinstance(request, core.Withdraw):
        host.refused.add(request.target)  # ty: ignore[invalid-argument-type]
        return lambda r: r.fail(asked, core.FailureReason.REFUSED)
    return lambda r: r.fail(asked, core.FailureReason.TIMED_OUT)


def _reply(host: Host, asked: Asked, answer: int) -> Callable[[FlowRun[Config]], None]:
    """The host's good answer to ``asked``, by what was asked."""
    request = asked.request

    def reply(run: FlowRun[Config]) -> None:
        match request:
            case core.RequestTurn():
                run.reply(asked, Plan(n=answer))
            case core.StartAttempt():
                ref = run.open_workspace(asked)
                if run is host.live:
                    host.opened.append(ref)
            case core.Withdraw(target=core.AttemptRef() as target):
                run.close_workspace(asked)
                if run is host.live:
                    host.settled[target] += 1
            case core.Measure(scope=scope):
                result = core.MeasurementResult(
                    scope=scope, evidence=(), status=core.ObservationStatus.SUCCEEDED
                )
                run.answer(asked, core.MeasurementFinished(decision=asked.decision, result=result))
            case core.Operation():
                verdict = core.ParentVerification(status="succeeded", verified=answer > 0)
                run.finish_operation(asked, verdict)
            case _:
                run.fail(asked, core.FailureReason.TIMED_OUT)

    return reply
