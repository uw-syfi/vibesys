"""``Flow`` (the ``ctx`` of a flow) and ``Group``."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Self, overload

from vs_core._registry import ContractError
from vs_core.types.common import Capabilities, Limits, RevisionRef, Seconds, TaskTerms, Value
from vs_core.types.settlement import RetainedCandidate, TrustedBaseline

from ._agents import Agents
from ._defs import UNBOUNDED, _Task
from ._errors import Stopped, TaskStopped
from ._evaluation import Evaluation
from ._kit import Kit
from ._owners import Owners
from ._retry import Again, GaveUp, RetryPolicy, Tries
from ._sources import First, Latch, LatchKey, Source, StopSource, Task, Timer
from ._spans import AttemptOutcome
from ._winner import INPUT, Input, Kept
from ._workspaces import Workspaces

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from pydantic import BaseModel

    from ._run import _Run


class Clock:
    """The run clock, read through the journal."""

    def __init__(self, kit: Kit) -> None:
        """Bind the clock to its flow."""
        self._kit = kit

    def now(self) -> Seconds:
        """Seconds on the run clock."""
        return self._kit.now()


class RunTask(Value):
    """What the run is for and how much of it is left: the task as the flow reads it."""

    run_id: str
    """The run's identity: what keeps two runs' writes to one shared place apart."""
    objective: str
    baseline: RevisionRef
    deadline_at: Seconds
    limits: Limits
    terms: TaskTerms
    """What the task fixes about measuring and comparing candidates."""


class Flow:
    """The ``ctx`` every flow function receives. See ``docs/contributing/flows.md``."""

    def __init__(self, run: _Run, task: _Task) -> None:
        """Bind the handle to its run and its own task."""
        self._run = run
        self._kit = Kit(run, task)
        self.facts = run.facts
        """The run's facts, constant for the run."""
        self.agents = Agents(self._kit)
        """Sessions with agents: ``ctx.agents.session(role)`` and ``await session.turn(...)``."""
        self.workspaces = Workspaces(self._kit)
        """Workspaces: ``open`` as a resource, ``keep``, ``verify`` and ``kept``."""
        self.evaluation = Evaluation(self._kit)
        """Evaluation: ``measure`` a revision, read what the agents measured themselves."""
        self.owners = Owners(self._kit)
        """Operations of other libraries' owners the search registered: ``perform``."""
        self.clock = Clock(self._kit)

    # control flow

    @overload
    async def first[A](self, a: Source[A], /) -> A: ...

    @overload
    async def first[A, B](self, a: Source[A], b: Source[B], /) -> A | B: ...

    @overload
    async def first[A, B, C](self, a: Source[A], b: Source[B], c: Source[C], /) -> A | B | C: ...

    @overload
    async def first[A, B, C, D](
        self, a: Source[A], b: Source[B], c: Source[C], d: Source[D], /
    ) -> A | B | C | D: ...

    async def first(self, *sources: Source[object]) -> object:
        """The next value of whichever source is ready first; ties go to the earliest."""
        self._kit.stop_point()
        return await First(sources)

    def timer(self, at: Seconds) -> Timer:
        """A source that fires once, when the run clock reaches ``at``."""
        return self._kit.timer(at)

    def latch[T](self, key: LatchKey[T]) -> Latch[T]:
        """A handle on the run-wide latch ``key``: ``set`` it once, or wait for its value.

        For a fact one flow learns and the flows beside it wait for, such as a measurement every
        later step builds on. Each handle yields the value once; take one per wait.
        """
        return self._run.latch(key)

    @property
    def path(self) -> str:
        """Where this flow sits: empty for the root, ``"a/b/"`` for task ``b`` of task ``a``."""
        return self._kit.path

    @property
    def stopped(self) -> StopSource:
        """The operator's stop as a source: yields the ``StopRequested`` event, once."""
        return self._kit.task.stop

    @property
    def stop_requested(self) -> bool:
        """Whether a stop has arrived."""
        return self._run.stop is not None

    def group[T](self, *, limit: int | None = None, stops: bool = True) -> Group[T]:
        """A scope for concurrent tasks; leaving it waits for all of them."""
        return Group(self._run, self._kit, limit, stops=stops)

    async def retry[R, V](
        self, attempt: Callable[[Tries[R]], Awaitable[V | Again[R]]], policy: RetryPolicy
    ) -> V | GaveUp[R]:
        """Repeat ``attempt`` until it gives a value, at most ``policy.max_tries`` times."""
        trace, span = self._run.trace, self._kit.task.span_id
        reasons: list[R] = []
        for number in range(1, policy.max_tries + 1):
            trace.attempt_started(span, number, policy.max_tries)
            outcome = await attempt(Tries(number=number, reasons=tuple(reasons)))
            if not isinstance(outcome, Again):
                trace.attempt_ended(span, AttemptOutcome.DONE, None)
                return outcome
            reasons.append(outcome.reason)
            last = number == policy.max_tries
            ending = AttemptOutcome.GAVE_UP if last else AttemptOutcome.AGAIN
            trace.attempt_ended(span, ending, str(outcome.reason))
            if policy.backoff is not None and number < policy.max_tries:
                await self.timer(self._kit.now() + policy.backoff.delay(number - 1))
        return GaveUp(reasons=tuple(reasons))

    def checkpoint(self, snapshot: object) -> None:
        """Record ``snapshot``: where a code change may resume this flow (see ``flow``)."""
        task = self._kit.task
        definition = task.flow
        if definition is None:
            raise ContractError(("checkpoint",), "only a registered flow has checkpoints")
        if not isinstance(snapshot, definition.snapshot):
            raise ContractError(
                ("checkpoint",), f"{definition.name} snapshots are {definition.snapshot.__name__}"
            )
        task.snapshot = snapshot
        task.checkpointed = True

    # the run

    def choose(self, candidate: Kept | Input) -> None:
        """Name the current winner: a kept candidate, or ``INPUT``. The last choice stands.

        The framework adopts the last choice when the run ends, however it ends.
        """
        if candidate is INPUT:
            self._run.choice = TrustedBaseline(revision=self.facts.baseline)
        elif isinstance(candidate, Kept):
            self._run.choice = RetainedCandidate(
                settlement_id=candidate.settlement.settlement_id, revision=candidate.revision
            )
        else:
            raise ContractError(("choose",), "choose a kept candidate or INPUT")

    def publish(self, record: BaseModel) -> None:
        """Replace the flow's published record: what status views and tests read of a run."""
        self._run.published = record.model_dump_json()

    # journaled reads

    @property
    def capabilities(self) -> Capabilities:
        """What the host offers this run (a journaled read)."""
        return self._kit.read(lambda view: view.capabilities, Capabilities)

    @property
    def task(self) -> RunTask:
        """The task of the run: objective, input, terms, deadline and limits (a journaled read)."""
        terms = self.facts.task
        if terms is None:
            message = "this run carries no task terms: the host must resolve them for the search"
            raise ContractError(("task",), message)
        return self._kit.read(
            lambda view: RunTask(
                run_id=view.run.owner.root,
                objective=view.facts.objective,
                baseline=view.facts.baseline,
                deadline_at=view.deadline_at,
                limits=view.limits,
                terms=terms,
            ),
            RunTask,
        )


class Group[T](Source[T | TaskStopped]):
    """A scope for concurrent tasks and the source of their results, one each, as they finish.

    Leaving the ``async with`` waits for every task still running. When the operator stops the
    run, tasks of a group with ``stops=True`` get ``Stopped`` at their next await.
    """

    def __init__(self, run: _Run, owner: Kit, limit: int | None, *, stops: bool) -> None:
        """Make the group; ``Flow.group`` is the way in."""
        self._run = run
        self._owner = owner
        self._limit = limit
        self.stops = stops
        self.results: deque[T | TaskStopped] = deque()
        self.running = 0

    @property
    def free(self) -> int:
        """How many more tasks the limit allows."""
        return UNBOUNDED if self._limit is None else self._limit - self.running

    def start[R, *Ts](
        self, key: str, fn: Callable[[Flow, *Ts], Coroutine[Any, Any, R]], *args: *Ts
    ) -> Task[R]:
        """Start ``fn(ctx, *args)`` as the task ``key``; it runs to its first await at once.

        A key that has a live snapshot (after a code change) resumes that flow instead.
        """
        if self.free <= 0:
            raise ContractError(("group", "start"), "the group is full: read `free` first")
        return self._run.begin_task(self._owner.task, self, key, (fn, args))

    async def __aenter__(self) -> Self:
        """Enter the scope."""
        return self

    async def __aexit__(self, kind: type[BaseException] | None, *_: object) -> None:
        """Wait for every task (unless the body crashed: a defect ends the run anyway)."""
        if kind is not None and not issubclass(kind, Stopped):
            return
        with self._owner.shielded():
            while self.running:
                await self

    def __aiter__(self) -> Self:
        """Iterate the results as tasks finish."""
        return self

    async def __anext__(self) -> T | TaskStopped:
        """The next result; the iteration ends when no task is running and no result is left."""
        if not self.alive():
            raise StopAsyncIteration
        return await self

    def ready(self) -> bool:
        return bool(self.results)

    def take(self) -> T | TaskStopped:
        return self.results.popleft()

    def alive(self) -> bool:
        return bool(self.results) or self.running > 0

    def describe(self) -> str:
        return f"group({self.running} running)"
