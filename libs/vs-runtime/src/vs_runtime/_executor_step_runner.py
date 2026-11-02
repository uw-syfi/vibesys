"""A step that runs on its role's executor, and says why running it again is safe.

Thirteen request kinds have no runner of their own: the workspace, session and evaluation steps
that create, reset, release, retain or read something on the executor of their role
(``RequestExecutors``). ``run`` hands the step to that executor. A step with no result runs again,
so each kind declares, in a ``Repeat`` class, why a second run reaches the same state:

- ``ReadOnly``: the executor reads and writes nothing.
- ``FixedEnd``: the request fixes the state it creates or resets to, so a repeat reaches it again.
- ``ReleaseOrBind``: the executor keys the resource by the request, so releasing or binding it twice
  is a no-op.
- ``RetainByContent``: the executor writes a ref keyed by the content or label, so a repeat finds the
  ref it wrote. It is safe once every writer of the scope has a result, which is ``decide``'s rule
  and is not checked here.

The constructor refuses a kind with no declared class, and a kind that another runner owns. No class
leaves anything for ``clear`` to remove: the executor runs in this process, so an attempt that died
left only what the repeat reaches again, and one that is cancelled in this process is ended before
``run`` returns. The loop records an attempt before its I/O, so an attempt after the first proves an
earlier one may have run: ``run`` passes that to the executor as ``ExecutionContext.repeat``, and the
executor looks for what the earlier attempt left (a snapshot by its label, a release it already made)
instead of doing it twice. What guards a repeat is that and the executor itself: when it cannot
conclude (a precondition unmet, the host not holding authority) it answers with an observation that is
not terminal, and ``run`` raises, which the loop clears and runs again within the step's retry bound.

A step cancelled mid-execution ends its wait for the executor, not any thread the executor started;
the repeat of such a step reaches the same state, which is the one residual.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from vs_core.api import (
    CancelOwnedJob,
    CancelTurn,
    CloseAttemptScope,
    CloseSession,
    CollectEvidence,
    ContractError,
    DiscardWorkspace,
    EnsureSession,
    EnsureWorkspace,
    ObserveOwnedJob,
    RetainRevision,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    Until,
)
from vs_core.api.recovery import Finished
from vs_runtime._cancel_wait import cancelled_for, finished_before_cancel
from vs_runtime._core_requests import ExecutionContext, ExecutorRefusal, payload_digest_of
from vs_runtime._rerun_policy import ReadOnly
from vs_runtime._step_runner import Cleared

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import Observation, Request, RequestId
    from vs_core.api.recovery import StepResult
    from vs_runtime._core_requests import ExecutionOutcome, RequestExecutors
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import Clock


@dataclass(frozen=True)
class FixedEnd:
    """Creates or resets something to the state the request fixes."""


@dataclass(frozen=True)
class ReleaseOrBind:
    """Releases or binds a resource the executor keys by the request."""


@dataclass(frozen=True)
class RetainByContent:
    """Writes a retention ref keyed by content or label, after the scope's writers have results."""


type Repeat = ReadOnly | FixedEnd | ReleaseOrBind | RetainByContent

DECLARED_REPEATS: Mapping[type[Request], Repeat] = MappingProxyType(
    {
        ObserveOwnedJob: ReadOnly(),
        CollectEvidence: ReadOnly(),
        EnsureWorkspace: FixedEnd(),
        DiscardWorkspace: ReleaseOrBind(),
        CloseAttemptScope: ReleaseOrBind(),
        EnsureSession: ReleaseOrBind(),
        CloseSession: ReleaseOrBind(),
        CancelTurn: ReleaseOrBind(),
        CancelOwnedJob: ReleaseOrBind(),
        SnapshotAndRetain: RetainByContent(),
        RetainRevision: RetainByContent(),
        SnapshotAndRetainRun: RetainByContent(),
    }
)
"""The class of each kind this runner serves: the closed set of kinds with no runner of their own."""


class ExecutorRefusedError(Exception):
    """The role's executor is not bound, so the step has no result and no repeat will give one."""

    def __init__(self, refusal: ExecutorRefusal) -> None:
        """Name the step and what the role lacks."""
        super().__init__(f"{refusal.request_id} was refused: {refusal.detail}")
        self.refusal = refusal


class StepWithoutResultError(Exception):
    """The executor could not conclude the step, so the attempt has no result."""

    def __init__(self, tag: RequestId, observation: Observation) -> None:
        """Name the step and what the executor observed."""
        super().__init__(
            f"attempt of {tag} ended without a result: {observation.status.value}"
            f" ({observation.diagnostic})"
        )
        self.tag = tag
        self.observation = observation


class ExecutorStepRunner:
    """Runs the steps of the kinds in ``DECLARED_REPEATS`` on the executor of their role."""

    def __init__(
        self,
        executors: RequestExecutors,
        repeats: Mapping[type[Request], Repeat],
        *,
        clock: Clock,
    ) -> None:
        """Bind the executors and the class each served kind declares.

        Raises ``ContractError`` naming the kind when one of the served kinds declares no class,
        or when a kind another runner owns is declared.
        """
        _check_repeats(repeats)
        self._executors = executors
        self._repeats = dict(repeats)
        self._clock = clock

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run the step on its executor; ``Cancelled`` once *cancel* is set and the executor ended."""
        self._served(step, tag)
        context = ExecutionContext(
            now_at=self._clock.now(),
            until=Until(at=deadline_at),
            payload_digest=payload_digest_of(step),
            repeat=attempt > 1,
        )
        work = asyncio.ensure_future(self._executors.dispatch(step, context))
        if not await finished_before_cancel(work, cancel):
            return cancelled_for(cancel)
        return _finished(work.result(), tag)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Report that nothing of an earlier attempt needs removing: every declared class repeats as is."""
        self._served(step, tag)
        return Cleared()

    def _served(self, step: Request, tag: RequestId) -> None:
        """Refuse a step of a kind this runner does not serve, or one whose id is not the tag."""
        if type(step) not in self._repeats:
            raise ContractError(("step", type(step).__name__), "not a kind this runner serves")
        if step.request_id != tag:
            raise ContractError(("step", str(tag)), "the tag is not the step's request id")


def _check_repeats(repeats: Mapping[type[Request], Repeat]) -> None:
    for kind in DECLARED_REPEATS:
        if kind not in repeats:
            raise ContractError(("repeat", kind.__name__), "declares no class of repeat")
    for kind in repeats:
        if kind not in DECLARED_REPEATS:
            raise ContractError(("repeat", kind.__name__), "is run by another runner")


def _finished(outcome: ExecutionOutcome, tag: RequestId) -> Finished:
    """The step's result: what the executor observed, which must be final, and its owner events."""
    if isinstance(outcome, ExecutorRefusal):
        raise ExecutorRefusedError(outcome)
    observed = outcome.observation
    if not observed.observation.terminal:
        raise StepWithoutResultError(tag, observed.observation)
    return Finished(
        observation=observed.observation, observed=observed, owner_events=outcome.owner_events
    )
