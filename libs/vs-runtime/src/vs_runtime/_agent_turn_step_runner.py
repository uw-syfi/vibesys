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
The tag is the step's request id, the same in every epoch, and ``clear`` is given the step the log
holds, so a restarted process finds the session, invocation and workspace an earlier attempt used
from the step itself, with no index of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import (
    AgentSessionKey,
    Completed,
    InvalidResponse,
    SessionPersistenceError,
)
from vs_core.api import (
    DispatchTurn,
    ExecuteRegisteredOperation,
    LifecycleClass,
    ObservationStatus,
    ResumeSessionTurn,
    TurnObserved,
    Until,
)
from vs_core.api.recovery import Cancelled, Finished
from vs_runtime._access_settlement import AccessKey, AccessSettlementError
from vs_runtime._core_identity import core_identity
from vs_runtime._core_requests import ExecutionContext
from vs_runtime._session_requests import load_session_binding, session_binding_key
from vs_runtime._stage_marks import agent_mark
from vs_runtime._step_runner import Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_agent.api import AgentSessions
    from vs_core.api import HostFence, Observation, Request, RequestId, TurnSpec
    from vs_core.api.recovery import StepResult
    from vs_runtime._core_requests import ExecutionResult
    from vs_runtime._local_job_step_runner import StageWorkReaper
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._session_lifecycle_requests import SessionLifecycleRequests
    from vs_runtime._session_requests import RuntimeSessionRequests
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import BlockingRunner, Clock

_WORKSPACE = "the workspace"
_JOURNAL = "the invocation journal"


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


class OperationTurns(Protocol):
    """Names the turn a registered ``SESSION_TURN`` operation runs as."""

    def turn(self, operation: ExecuteRegisteredOperation) -> DispatchTurn | None:
        """The ``DispatchTurn`` *operation* runs as, or ``None`` when it is no turn.

        The turn carries the operation's own request id, so what the turn path observes is
        observed under the step core asked for. In production it is the registry's
        ``normalize_turn`` of the decoded operation, with the reserved inputs resolved.
        """


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
    start: Callable[[ExecutionContext, str], Awaitable[ExecutionResult]]
    """Starts one attempt under the execution context and the mark the runner builds for it."""


@dataclass(frozen=True)
class RunWriter:
    """The one writer of a run: its id, and the host fence it holds.

    The run's lock makes one process the only writer, so the fence does not change within a
    run. The id names the run in the mark of every agent process, because two runs on one host
    can reuse a request id.
    """

    run_id: str
    fence: HostFence


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
        clock: Clock,
        writer: RunWriter,
        reaper: StageWorkReaper,
    ) -> None:
        """Bind the turn executors, the durable state they write, the run clock, the writer and the reaper.

        *state* is what the executors were built with. *reaper* ends the processes of a step by its
        tag (the environment's: containers, or host processes found by their mark). *clock* is the
        run clock the deadline of an attempt is read against.
        """
        self._paths = paths
        self._sessions = state.sessions
        self._store = state.store
        self._blocking = state.blocking
        self._clock = clock
        self._writer = writer
        self._reaper = reaper

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Run attempt *attempt* of the turn; ``Cancelled`` once *cancel* is set and the turn ended."""
        del attempt
        plan = self._plan(step, tag)
        context = ExecutionContext(
            fence=self._writer.fence,
            now_at=self._clock.now(),
            until=Until(at=deadline_at),
            payload_digest=hashlib.sha256(step.model_dump_json().encode()).hexdigest(),
        )
        mark = agent_mark(self._writer.run_id, tag)
        execution = asyncio.ensure_future(plan.start(context, mark))
        try:
            return await self._ended(execution, plan, tag, cancel)
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """End everything carrying *tag*, then make the next attempt start from the recorded state."""
        await self._clear(self._plan(step, tag), tag)
        return Cleared()

    def _plan(self, step: Request, tag: RequestId) -> _Plan:
        """The turn *step* is, or ``NotATurnError``; *tag* must be the step's own request id."""
        if step.request_id != tag:
            message = f"the tag {tag} is not the step's request id {step.request_id}"
            raise ValueError(message)
        paths = self._paths
        match step:
            case DispatchTurn():
                return _Plan(
                    step.turn,
                    step,
                    lambda context, mark: paths.dispatch.attempt(step, context, mark=mark),
                )
            case ResumeSessionTurn():
                return _Plan(
                    step.turn,
                    step,
                    lambda context, mark: paths.resume.attempt(step, context, mark=mark),
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
                    lambda context, mark: paths.dispatch.attempt(
                        dispatch, context, mark=mark, behalf=core_identity(step)
                    ),
                )
            case _:
                raise NotATurnError(step)

    def _journaled(self, plan: _Plan) -> tuple[str, AgentSessionKey, str] | None:
        """The binding key, session key and invocation the turn uses, or ``None`` before any session."""
        bkey = session_binding_key(plan.binding_of, plan.spec.session.session_id)
        binding = load_session_binding(self._store, bkey)
        if binding is None:
            return None
        return bkey, AgentSessionKey.parse(binding.session_key), plan.spec.invocation_id.root

    async def _ended(
        self,
        execution: asyncio.Future[ExecutionResult],
        plan: _Plan,
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
        journaled = self._journaled(plan)
        if journaled is not None:
            _, key, invocation = journaled
            await self._blocking.run(self._sessions.cancel, key, invocation)
        (outcome,) = await asyncio.gather(execution, return_exceptions=True)
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
        if not isinstance(outcome, BaseException) and _answered(outcome):
            return _finished(outcome, tag)
        await self._clear(plan, tag)
        return Cancelled(reason=reason)

    async def _clear(self, plan: _Plan, tag: RequestId) -> None:
        await self._reaper.reap(tag)
        journaled = self._journaled(plan)
        if journaled is None:
            return
        bkey, key, invocation = journaled
        try:
            outcome = await self._blocking.run(self._sessions.inspect, key, invocation)
            if isinstance(outcome, Completed | InvalidResponse):
                return
            await self._paths.dispatch.settlement.rewind(
                AccessKey(binding=bkey, invocation=invocation)
            )
        except AccessSettlementError as error:
            raise ClearUnavailableError(tag, _WORKSPACE) from error
        try:
            await self._blocking.run(self._sessions.forget_unfinished, key, invocation)
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
