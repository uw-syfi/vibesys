"""CancelTurn, CloseSession and ResumeSessionTurn over durable sessions and the Fake agent client."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.executor_context import RevocableLease, context_for
from tests.support.observation_contract import assert_core_accepts
from tests.support.session_lifecycle_world import (
    cancel_request,
    cancelled_keys,
    close_request,
    lifecycle_executor,
    open_lifecycle_host,
    released_keys,
    resume_request,
)
from tests.support.session_world import SessionHost, dispatch_request, ensure_request

from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_core.api import ContinuationId, ObservationStatus, SessionObserved, TurnObserved
from vs_project.api import Project
from vs_runtime.api.core import ExecutionResult, ReceiptStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_core.api import RequestBase


class World:
    """One Project directory and host; ``execute`` is a host (re)start over the same disk."""

    def __init__(self, base: Path) -> None:
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        sessions = self._project.state.state_store_namespace("sessions")
        self.host: SessionHost = open_lifecycle_host(
            base / "workspace",
            DurableSessionStore(sessions.slot("sessions.json", AgentSessionState)),
        )

    def store(self) -> ReceiptStore:
        return ReceiptStore(self._project.state.state_store_namespace("run"))

    async def execute(
        self, request: RequestBase, *, lease: RevocableLease | None = None
    ) -> ExecutionResult:
        context = context_for(request, lease=lease)
        outcome = await lifecycle_executor(self.host, self.store()).execute(
            cast("Any", request), context
        )
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome

    async def started(self) -> None:
        """Ensure the session and complete one turn, so a conversation is retained."""
        assert status(await self.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        assert status(await self.execute(dispatch_request())) is ObservationStatus.SUCCEEDED


@asynccontextmanager
async def world() -> AsyncIterator[World]:
    with tempfile.TemporaryDirectory() as raw:
        yield World(Path(raw))


def status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


# CancelTurn


@pytest.mark.asyncio
async def test_cancel_of_a_finished_turn_is_an_inspected_stop_and_replays() -> None:
    async with world() as w:
        await w.started()
        first = await w.execute(cancel_request())
        again = await w.execute(cancel_request())
        observed = first.observation.observation
        assert observed.status is ObservationStatus.CANCELLED
        assert observed.terminal
        assert observed.accepted
        assert observed.released
        assert observed.children_complete
        assert again == first
        assert cancelled_keys(w.host) == 0, "nothing was running to cancel"
        assert_core_accepts([first, again], expect_retry=False)


@pytest.mark.asyncio
async def test_cancel_of_an_interrupted_turn_releases_it_without_dispatching_again() -> None:
    async with world() as w:
        assert status(await w.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        w.host.faults.down = True
        lost = await w.execute(dispatch_request())
        assert status(lost) is ObservationStatus.UNKNOWN
        w.host.faults.down = False
        cancelled = await w.execute(cancel_request())
        assert status(cancelled) is ObservationStatus.CANCELLED
        assert cancelled.observation.observation.released
        assert len(w.host.turns) == 1, "cancel never re-dispatches"
        assert_core_accepts([lost, cancelled], expect_retry=False)


@pytest.mark.asyncio
async def test_cancel_of_an_undispatched_invocation_or_unknown_session_is_rejected() -> None:
    async with world() as w:
        never_ensured = await w.execute(cancel_request("req-a"))
        assert status(never_ensured) is ObservationStatus.REJECTED
        await w.execute(ensure_request())
        undispatched = await w.execute(cancel_request("req-b", invocation="inv-9"))
        assert status(undispatched) is ObservationStatus.REJECTED
        assert undispatched.observation.observation.terminal
        assert cancelled_keys(w.host) == 0


# CloseSession


@pytest.mark.asyncio
async def test_close_releases_the_lease_once_and_keeps_the_conversation_for_resume() -> None:
    async with world() as w:
        await w.started()
        closed = await w.execute(close_request())
        again = await w.execute(close_request())
        observed = closed.observation.observation
        assert observed.status is ObservationStatus.SUCCEEDED
        assert observed.released
        assert again == closed
        assert released_keys(w.host) == 1
        assert [type(e) for e in closed.owner_events] == [SessionObserved]
        assert_core_accepts([closed, again], expect_retry=False)
        resumed = await w.execute(resume_request())
        assert status(resumed) is ObservationStatus.SUCCEEDED
        assert len(w.host.turns) == 2
        assert w.host.turns[1].expected_provider_session_id is not None


@pytest.mark.asyncio
async def test_close_of_a_session_that_was_never_ensured_is_rejected() -> None:
    async with world() as w:
        closed = await w.execute(close_request())
        assert status(closed) is ObservationStatus.REJECTED
        assert closed.observation.observation.terminal
        assert released_keys(w.host) == 0


# ResumeSessionTurn


@pytest.mark.asyncio
async def test_resume_continues_the_retained_conversation_with_exact_identity() -> None:
    async with world() as w:
        await w.started()
        request = resume_request()
        first = await w.execute(request)
        again = await w.execute(request)
        assert status(first) is ObservationStatus.SUCCEEDED
        assert again == first
        (event,) = first.owner_events
        assert isinstance(event, TurnObserved)
        assert event.invocation.invocation_id == request.turn.invocation_id
        assert event.output_json is not None
        assert event.observation == first.observation.observation
        assert len(w.host.turns) == 2, "a repeat replays instead of dispatching"
        assert_core_accepts([first, again], expect_retry=False)


@pytest.mark.asyncio
async def test_a_continuation_has_one_successor() -> None:
    async with world() as w:
        await w.started()
        assert status(await w.execute(resume_request())) is ObservationStatus.SUCCEEDED
        other = await w.execute(resume_request("req-resume-2", invocation="inv-3"))
        assert status(other) is ObservationStatus.REJECTED
        assert other.observation.observation.terminal
        assert len(w.host.turns) == 2


@pytest.mark.asyncio
async def test_resume_without_a_retained_turn_or_session_is_rejected() -> None:
    async with world() as w:
        missing = await w.execute(resume_request("req-a"))
        assert status(missing) is ObservationStatus.REJECTED
        await w.execute(ensure_request())
        fresh = await w.execute(resume_request("req-b"))
        assert status(fresh) is ObservationStatus.REJECTED
        assert not w.host.turns


@pytest.mark.asyncio
async def test_resume_needs_charge_class_resume_and_a_matching_continuation() -> None:
    async with world() as w:
        await w.started()
        request = resume_request()
        free = request.model_copy(
            update={"turn": request.turn.model_copy(update={"charge_class": "free"})}
        )
        assert status(await w.execute(free)) is ObservationStatus.REJECTED
        skewed = resume_request("req-skew").model_copy(
            update={"continuation_id": ContinuationId(root="other")}
        )
        assert status(await w.execute(skewed)) is ObservationStatus.REJECTED
        assert len(w.host.turns) == 1


# the shared contract


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "close", "resume"])
async def test_a_stale_host_performs_nothing_and_a_later_host_completes(kind: str) -> None:
    async with world() as w:
        await w.started()
        request = {"cancel": cancel_request, "close": close_request, "resume": resume_request}[
            kind
        ]()
        lost = RevocableLease()
        lost.valid = False
        stale = await w.execute(request, lease=lost)
        assert status(stale) is ObservationStatus.UNKNOWN
        assert not stale.observation.observation.terminal
        assert len(w.host.turns) == 1
        assert released_keys(w.host) == cancelled_keys(w.host) == 0
        later = await w.execute(request)
        assert later.observation.observation.terminal
        assert_core_accepts([stale, later], expect_retry=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "close", "resume"])
async def test_another_payload_under_one_request_identity_is_rejected(kind: str) -> None:
    async with world() as w:
        await w.started()
        request = {"cancel": cancel_request, "close": close_request, "resume": resume_request}[
            kind
        ]()
        first = await w.execute(request)
        turns = len(w.host.turns)
        context = context_for(request).model_copy(update={"payload_digest": "another-payload"})
        outcome = await lifecycle_executor(w.host, w.store()).execute(cast("Any", request), context)
        assert isinstance(outcome, ExecutionResult)
        assert status(outcome) is ObservationStatus.REJECTED
        assert len(w.host.turns) == turns
        assert_core_accepts([first, outcome], expect_retry=False)


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(repeats=st.integers(min_value=1, max_value=4), close_first=st.booleans())
@pytest.mark.asyncio
async def test_repeating_lifecycle_requests_never_adds_effects(
    *, repeats: int, close_first: bool
) -> None:
    async with world() as w:
        await w.started()
        if close_first:
            await w.execute(close_request())
        results = [await w.execute(resume_request()) for _ in range(repeats)]
        assert all(r == results[0] for r in results)
        assert len(w.host.turns) == 2
        assert released_keys(w.host) == int(close_first)
