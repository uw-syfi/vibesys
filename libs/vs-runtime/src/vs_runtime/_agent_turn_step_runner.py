"""A step runner for one agent turn, whichever kind of request asked for it.

Three kinds of step are one agent turn: ``DispatchTurn``, ``ResumeSessionTurn`` (the turn that
continues a retained conversation) and a registered operation of lifecycle ``SESSION_TURN``
(which an ``OperationTurns`` resolves to the ``DispatchTurn`` it runs as). ``run`` asks the
session executor for one attempt of the turn: the turn path that resolves the session, enforces
the role's workspace access and calls the provider through the invocation journal. Every agent
process the turn starts carries the step's tag (``vs_agent.step_tags``).

``clear`` is what makes the next attempt start clean. A turn never adopts: whatever an earlier
attempt left is ended, not taken over. ``clear`` kills the processes carrying the tag, then (when
the journal holds no answer for the invocation) returns the workspace to the baseline the access
receipt retains and forgets the unanswered journal record, so the invocation is dispatched
again. An answered invocation is left alone: the journal replays it, and its writes were judged.
The tag is the step's request id, the same in every epoch, so a restarted process finds what an
earlier one started by the tag alone: ``run`` records, before any I/O, which session and
invocation the tag names.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from vs_agent.api import (
    AgentSessionKey,
    Completed,
    InvalidResponse,
    OrphanReapError,
    SessionPersistenceError,
)
from vs_core.api import (
    DispatchTurn,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    ResumeSessionTurn,
    TurnObserved,
)
from vs_core.api.recovery import Cancelled, Finished
from vs_runtime._access_settlement import AccessKey, AccessSettlementError
from vs_runtime._core_identity import core_identity
from vs_runtime._session_requests import load_session_binding, session_binding_key
from vs_runtime._step_runner import Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentSessions
    from vs_core.api import Observation, Request, RequestId, TurnSpec
    from vs_core.api.recovery import StepResult
    from vs_runtime._core_requests import ExecutionContext, ExecutionResult
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._session_lifecycle_requests import SessionLifecycleRequests
    from vs_runtime._session_requests import RuntimeSessionRequests
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import BlockingRunner

_STEP_TURNS = "step-turns"
_AGENTS = "the agent containers"
_WORKSPACE = "the workspace"
_JOURNAL = "the invocation journal"

type StepReaper = Callable[[str], None]
"""Kill every agent process carrying the tag it is given, or raise ``OrphanReapError``.

It blocks, so the runner calls it through its ``BlockingRunner``. In production it is
``vs_agent.reap_step_agents`` bound to the run's id and Docker client.
"""


class NotATurnError(TypeError):
    """The step is not an agent turn: this runner runs no other kind."""

    def __init__(self, step: Request) -> None:
        """Name the step kind."""
        super().__init__(f"AgentTurnStepRunner runs agent turns, not {step.kind}")


class TurnWithoutResultError(Exception):
    """The turn path ended the attempt without a result, so the attempt has none."""

    def __init__(self, tag: RequestId, observation: Observation) -> None:
        """Name the step and what the turn path observed."""
        super().__init__(
            f"attempt of {tag} ended without a result: {observation.status.value}"
            f" ({observation.diagnostic})"
        )
        self.tag = tag
        self.observation = observation


class StepTurn(BaseModel):
    """Which session and invocation a step's tag started, recorded before the turn begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    binding: str
    session_key: str
    invocation: str


class OperationTurns(Protocol):
    """Names the turn a registered ``SESSION_TURN`` operation runs as."""

    def turn(self, operation: ExecuteRegisteredOperation) -> DispatchTurn | None:
        """The ``DispatchTurn`` *operation* runs as, or ``None`` when it is no turn.

        The turn carries the operation's own request id, so what the turn path observes is
        observed under the step core asked for. In production it is the registry's
        ``normalize_turn`` of the decoded operation, with the reserved inputs resolved.
        """
        ...


@dataclass(frozen=True)
class TurnPaths:
    """The executors that run the three turn-shaped kinds of step."""

    dispatch: RuntimeSessionRequests
    resume: SessionLifecycleRequests
    operations: OperationTurns


@dataclass(frozen=True)
class _Plan:
    """One step as one turn: what it names in the journal, and how an attempt of it starts."""

    spec: TurnSpec
    binding_of: Request
    start: Callable[[], Awaitable[ExecutionResult]]


@dataclass(frozen=True)
class TurnState:
    """The durable state the turn path writes, and the runner that keeps blocking calls off the loop."""

    sessions: AgentSessions
    store: ReceiptStore
    blocking: BlockingRunner


class AgentTurnStepRunner:
    """Runs a ``DispatchTurn`` through the session executor, and clears what an attempt left."""

    def __init__(
        self,
        paths: TurnPaths,
        state: TurnState,
        contexts: Callable[[Request], ExecutionContext],
        reap: StepReaper,
    ) -> None:
        """Bind the turn executors, the durable state they write, the contexts of attempts and the reaper.

        *state* is what the executors were built with. *contexts* gives the execution context
        of an attempt of a step.
        """
        self._paths = paths
        self._sessions = state.sessions
        self._store = state.store
        self._blocking = state.blocking
        self._contexts = contexts
        self._reap = reap

    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        """Run attempt *attempt* of the turn; ``Cancelled`` once *cancel* is set and the turn ended."""
        del attempt
        plan = self._plan(step)
        if step.request_id != tag:
            message = f"the tag {tag} is not the step's request id {step.request_id}"
            raise ValueError(message)
        started = self._note(plan, tag)
        execution = asyncio.ensure_future(plan.start())
        try:
            return await self._ended(execution, started, tag, cancel)
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    def _plan(self, step: Request) -> _Plan:
        """The turn *step* is, or ``NotATurnError``."""
        paths = self._paths
        match step:
            case DispatchTurn():
                return _Plan(
                    step.turn,
                    step,
                    lambda: paths.dispatch.attempt(step, self._contexts(step)),
                )
            case ResumeSessionTurn():
                return _Plan(
                    step.turn, step, lambda: paths.resume.attempt(step, self._contexts(step))
                )
            case ExecuteRegisteredOperation() if (
                step.operation.schema_ref.lifecycle is LifecycleClass.SESSION_TURN
            ):
                dispatch = paths.operations.turn(step)
                if dispatch is None:
                    raise NotATurnError(step)
                if dispatch.request_id != step.request_id:
                    message = f"the turn of {step.request_id} carries the request id {dispatch.request_id}"
                    raise ValueError(message)
                return _Plan(
                    dispatch.turn,
                    step,
                    lambda: paths.dispatch.attempt(
                        dispatch, self._contexts(step), behalf=core_identity(step)
                    ),
                )
            case _:
                raise NotATurnError(step)

    async def clear(self, tag: RequestId) -> Adopted | Cleared:
        """End everything carrying *tag*, then make the next attempt start from the recorded state."""
        await self._clear(tag)
        return Cleared()

    def _note(self, plan: _Plan, tag: RequestId) -> StepTurn | None:
        """Record, before the turn can begin, which session and invocation *tag* names."""
        bkey = session_binding_key(plan.binding_of, plan.spec.session.session_id)
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return None
        started = StepTurn(
            binding=bkey,
            session_key=binding.session_key,
            invocation=plan.spec.invocation_id.root,
        )
        self._store.record_once(_STEP_TURNS, "turn", tag.root, started)
        return started

    async def _ended(
        self,
        execution: asyncio.Future[ExecutionResult],
        started: StepTurn | None,
        tag: RequestId,
        cancel: CancelSignal,
    ) -> StepResult:
        """Wait for the attempt to end by itself or for the signal to ask it to."""
        asked = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({execution, asked}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            asked.cancel()
        if execution.done() and not cancel.is_set():
            return _finished(execution.result(), tag)
        reason = await cancel.wait()
        if started is not None:
            key = AgentSessionKey.parse(started.session_key)
            await self._blocking.run(self._sessions.cancel, key, started.invocation)
        (outcome,) = await asyncio.gather(execution, return_exceptions=True)
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, BaseException) and _answered(outcome):
            return _finished(outcome, tag)
        await self._clear(tag)
        return Cancelled(reason=reason)

    async def _clear(self, tag: RequestId) -> None:
        try:
            await self._blocking.run(self._reap, tag.root)
        except OrphanReapError as error:
            raise ClearUnavailableError(tag, _AGENTS) from error
        started = self._store.load(_STEP_TURNS, "turn", tag.root, StepTurn)
        if started is None:
            return
        key = AgentSessionKey.parse(started.session_key)
        try:
            outcome = await self._blocking.run(self._sessions.inspect, key, started.invocation)
            if isinstance(outcome, Completed | InvalidResponse):
                return
            await self._paths.dispatch.settlement.rewind(
                AccessKey(binding=started.binding, invocation=started.invocation)
            )
        except AccessSettlementError as error:
            raise ClearUnavailableError(tag, _WORKSPACE) from error
        try:
            await self._blocking.run(self._sessions.forget_unfinished, key, started.invocation)
        except SessionPersistenceError as error:
            raise ClearUnavailableError(tag, _JOURNAL) from error


def _answered(result: ExecutionResult) -> bool:
    """Whether the attempt ended with an answer rather than because it was stopped."""
    observation = result.observation.observation
    return observation.terminal and observation.status is not ObservationStatus.CANCELLED


def _finished(result: ExecutionResult, tag: RequestId) -> Finished:
    """The step's result: the observation, with the turn's reply carried in the report."""
    observed = result.observation
    observation = observed.observation
    if not observation.terminal:
        raise TurnWithoutResultError(tag, observation)
    turn = next((event for event in result.owner_events if isinstance(event, TurnObserved)), None)
    if turn is not None:
        observed = observed.model_copy(
            update={
                "outcome_schema": turn.output_schema,
                "outcome_json": turn.output_json,
                "suspension": turn.suspension,
            }
        )
    return Finished(observation=observation, observed=observed)
