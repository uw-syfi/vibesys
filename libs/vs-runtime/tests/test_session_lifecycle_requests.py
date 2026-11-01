"""CancelTurn, CloseSession and ResumeSessionTurn over durable sessions and the Fake agent client."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.session_lifecycle_world import (
    TurnGate,
    cancel_request,
    cancelled_keys,
    close_request,
    lifecycle_executor,
    open_lifecycle_host,
    released_keys,
    resume_request,
    run_snapshot_request,
)
from tests.support.session_world import (
    SessionHost,
    dispatch_request,
    ensure_request,
    inspect_request,
)

from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_agent.api.testing import FakeAgentSessions
from vs_core.api import (
    Continuation,
    ContinuationId,
    ContinuationPhase,
    DecisionId,
    DispatchTurn,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    ResourceId,
    SessionObserved,
    TurnObserved,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    ExecutionResult,
    JournalRunInvocations,
    ReceiptStore,
    ReleasedRunInvocations,
)
from vs_runtime.api.executor_contexts import RevocableLease, context_for
from vs_runtime.api.observation_contracts import assert_core_accepts
from vs_sim.api import OsThreads
from vs_sim.api.testing import examples, wait_until_started

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_core.api import RequestBase


class World:
    """One Project directory and host; ``execute`` is a host (re)start over the same disk."""

    def __init__(self, base: Path, gate: TurnGate | None = None) -> None:
        self.base = base
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        sessions = self._project.state.state_store_namespace("sessions")
        self.checkpoints = DurableSessionStore(
            sessions.slot("sessions.json", AgentSessionState), threads=OsThreads()
        )
        self.sessions: FakeAgentSessions | None = None
        self.host: SessionHost = open_lifecycle_host(
            base / "workspace", self.checkpoints, gate=gate
        )

    def store(self) -> ReceiptStore:
        return ReceiptStore(self._project.state.state_store_namespace("run"), threads=OsThreads())

    async def execute(
        self, request: RequestBase, *, lease: RevocableLease | None = None
    ) -> ExecutionResult:
        context = context_for(request, lease=lease)
        outcome = await lifecycle_executor(self.host, self.store(), self.sessions).execute(
            cast("Any", request), context
        )
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome

    async def started(self) -> None:
        """Ensure the session and complete one turn, so a conversation is retained."""
        assert status(await self.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        assert status(await self.execute(dispatch_request())) is ObservationStatus.SUCCEEDED


@asynccontextmanager
async def world(gate: TurnGate | None = None) -> AsyncIterator[World]:
    with tempfile.TemporaryDirectory() as raw:
        yield World(Path(raw), gate)


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
async def test_cancel_of_a_running_turn_is_unknown_until_it_ends() -> None:
    gate = TurnGate()
    async with world(gate) as w:
        w.sessions = FakeAgentSessions(
            w.host.client, w.host.journal, threads=OsThreads()
        )  # one live process
        assert status(await w.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        running = asyncio.create_task(w.execute(dispatch_request()))
        await wait_until_started(gate.started, running)
        try:
            early = await w.execute(cancel_request())
            assert status(early) is ObservationStatus.UNKNOWN
            assert not early.observation.observation.terminal
            assert cancelled_keys(w.host) == 1, "the stop was requested, not assumed"
        finally:
            gate.proceed.set()
        assert status(await running) is ObservationStatus.SUCCEEDED
        late = await w.execute(cancel_request())
        assert status(late) is ObservationStatus.CANCELLED
        assert late.observation.observation.released
        assert late.observation.observation.sequence > early.observation.observation.sequence
        assert_core_accepts([early, late], expect_retry=True)


@pytest.mark.asyncio
async def test_a_restarted_host_cannot_claim_a_cancelled_turn_it_never_ran() -> None:
    gate = TurnGate()
    async with world(gate) as w:
        assert status(await w.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        w.sessions = FakeAgentSessions(w.host.client, w.host.journal, threads=OsThreads())
        running = asyncio.create_task(w.execute(dispatch_request()))
        await wait_until_started(gate.started, running)
        w.sessions = None  # the next call starts a new host over the same journal
        try:
            cancelled = await w.execute(cancel_request())
            assert status(cancelled) is ObservationStatus.UNKNOWN
            assert not cancelled.observation.observation.released
        finally:
            gate.proceed.set()
        await running


@pytest.mark.asyncio
async def test_a_turn_refused_before_the_provider_call_is_cancelled_and_frees_the_workspace() -> (
    None
):
    """A resume the restarted provider cannot continue left a dispatch record but no journal row.

    No provider call began in any host, so its cancellation is proven (not "its dispatch has not
    returned" for ever), and the access receipt it opened does not hold the workspace against the
    turn that follows.
    """
    async with world() as w:
        await w.started()
        w.host = w.host.restart_lost_conversation(w.host.resolver.workspace)
        refused = await w.execute(dispatch_request("req-two", "inv-2"))
        assert status(refused) is ObservationStatus.REJECTED
        cancelled = await w.execute(cancel_request("req-c2", invocation="inv-2"))
        assert status(cancelled) is ObservationStatus.CANCELLED
        assert cancelled.observation.observation.released
        later = await w.execute(dispatch_request("req-three", "inv-3"))
        assert "not judged" not in later.observation.observation.diagnostic
        assert w.host.turns == []


@pytest.mark.asyncio
async def test_cancel_of_an_undispatched_invocation_or_unknown_session_proves_nothing_held() -> (
    None
):
    """Core ends a cancelling turn only on a proven release; a bare rejection strands it."""
    async with world() as w:
        never_ensured = await w.execute(cancel_request("req-a"))
        assert status(never_ensured) is ObservationStatus.REJECTED
        assert holds_nothing(never_ensured)
        await w.execute(ensure_request())
        undispatched = await w.execute(cancel_request("req-b", invocation="inv-9"))
        assert status(undispatched) is ObservationStatus.REJECTED
        assert undispatched.observation.observation.terminal
        assert holds_nothing(undispatched)
        assert cancelled_keys(w.host) == 0


_STEPS = ("ensure", "cancel", "dispatch")


@pytest.mark.asyncio
@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(order=st.permutations(_STEPS), repeat_dispatch=st.booleans())
async def test_a_cancel_answered_as_nothing_held_is_never_followed_by_a_provider_call(
    order: list[str], *, repeat_dispatch: bool
) -> None:
    """In any order of ensure, cancel and dispatch, each a fresh host over the same disk.

    A cancellation that proves nothing is held fences the invocation: a dispatch that arrives
    later is refused before any provider call, so the answer stays true. A cancellation that
    comes after the dispatch reports the turn it stopped instead.
    """
    async with world() as w:
        answers: dict[str, ExecutionResult] = {}
        for step in order:
            match step:
                case "ensure":
                    answers[step] = await w.execute(ensure_request())
                case "cancel":
                    answers[step] = await w.execute(cancel_request("req-c", invocation="inv-1"))
                case _:
                    answers[step] = await w.execute(dispatch_request("req-d", "inv-1"))
        if repeat_dispatch:
            again = (await w.execute(dispatch_request("req-d", "inv-1"))).observation.observation
            first = answers["dispatch"].observation.observation
            assert (again.status, again.terminal, again.accepted, again.released) == (
                first.status,
                first.terminal,
                first.accepted,
                first.released,
            )
        cancel = answers["cancel"]
        if holds_nothing(cancel):
            assert status(cancel) is ObservationStatus.REJECTED
            assert w.host.turns == []
            assert status(answers["dispatch"]) is ObservationStatus.REJECTED
            assert answers["dispatch"].observation.observation.released
        else:
            assert order.index("dispatch") < order.index("cancel")
            assert cancel.observation.observation.released
            assert len(w.host.turns) == 1


@pytest.mark.asyncio
async def test_cancels_of_an_undispatched_invocation_under_two_identities_both_hold_nothing() -> (
    None
):
    """The first cancellation fences the invocation; a second one finds the fence and agrees."""
    async with world() as w:
        await w.execute(ensure_request())
        first = await w.execute(cancel_request("req-c1", invocation="inv-9"))
        second = await w.execute(cancel_request("req-c2", invocation="inv-9"))
        assert holds_nothing(first)
        assert holds_nothing(second)


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
        # Proof that nothing is held: a closing session in core ends on it instead of waiting.
        assert holds_nothing(closed)


_CLOSE_STEPS = ("ensure", "close")


@pytest.mark.asyncio
@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(order=st.permutations(_CLOSE_STEPS), again=st.booleans())
async def test_a_close_answered_as_nothing_held_is_never_followed_by_a_binding(
    order: list[str], *, again: bool
) -> None:
    """In either order of ensure and close of one episode, each a fresh host over the same disk.

    A close that finds nothing bound fences the episode: an ensure of that episode arriving
    later is refused, so no turn ever runs in the session, and a second close under another
    identity agrees. A later episode still acquires the session, because the fence names the
    episode, never the session alone. A close after the ensure releases what it bound.
    """
    async with world() as w:
        answers = {
            step: await w.execute(ensure_request() if step == "ensure" else close_request())
            for step in order
        }
        close = answers["close"]
        if again:
            other = await w.execute(close_request("req-close-2"))
            assert holds_nothing(other) == holds_nothing(close)
        if holds_nothing(close):
            assert order == ["close", "ensure"]
            assert status(answers["ensure"]) is ObservationStatus.REJECTED
            refused = await w.execute(dispatch_request("req-d", "inv-1"))
            assert status(refused) is ObservationStatus.REJECTED
            assert w.host.turns == []
            later = ensure_request("req-ensure-2").model_copy(
                update={"admission_id": DecisionId(root="episode-2")}
            )
            assert status(await w.execute(later)) is ObservationStatus.SUCCEEDED
        else:
            assert order == ["ensure", "close"]
            assert close.observation.observation.released


def holds_nothing(result: ExecutionResult) -> bool:
    """The answer proves release of an identity under which nothing was ever ensured."""
    observed = result.observation.observation
    return observed.released and observed.children_complete and not observed.accepted


def in_episode[R: RequestBase](request: R, generation: int) -> R:
    """The same request issued under another admission episode."""
    scope = request.scope.model_copy(update={"generation": generation})
    return request.model_copy(update={"scope": scope})


@pytest.mark.asyncio
async def test_close_releases_only_the_episode_that_holds_the_lease() -> None:
    async with world() as w:
        await w.started()
        stale = await w.execute(in_episode(close_request("req-stale"), 1))
        assert status(stale) is ObservationStatus.REJECTED
        assert released_keys(w.host) == 0
        owner = await w.execute(close_request())
        assert status(owner) is ObservationStatus.SUCCEEDED
        assert released_keys(w.host) == 1


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
        # A finished resume ends its writer like any dispatched turn, so the attempt can drain.
        assert event.observation.released
        assert event.observation.children_complete
        assert len(w.host.turns) == 2, "a repeat replays instead of dispatching"
        assert_core_accepts([first, again], expect_retry=False)


class _Waits:
    """The yield source of a host whose agent always asks to wait for one evaluation job."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def yielded(self, request: DispatchTurn) -> Continuation:
        self.asked.append(request.turn.invocation_id.root)
        here = InvocationRef(
            session_id=request.turn.session.session_id,
            invocation_id=request.turn.invocation_id,
            generation=request.scope.generation,
        )
        return Continuation(
            continuation_id=ContinuationId(root=f"{request.turn.invocation_id.root}/evaluation"),
            invocation=here,
            next_invocation=here.model_copy(
                update={"invocation_id": InvocationId(root=f"{here.invocation_id.root}/resume")}
            ),
            jobs=(ResourceId(root="job"),),
            deadline_at=100.0,
            phase=ContinuationPhase.WAITING,
        )


@pytest.mark.asyncio
async def test_a_resumed_turn_that_waits_again_is_observed_with_its_new_suspension() -> None:
    """Resuming a turn does not end its right to wait: its observation carries the new wait.

    An implementer that sees a measurement and changes its candidate asks for another one in
    the same turn. Dropping the suspension of the resumed dispatch left that wait unrecorded.
    """
    waits = _Waits()
    async with world() as w:
        await w.started()
        request = resume_request()
        result = await lifecycle_executor(w.host, w.store(), w.sessions, yields=waits).execute(
            cast("Any", request), context_for(request)
        )
        assert isinstance(result, ExecutionResult)
        (event,) = result.owner_events
        assert isinstance(event, TurnObserved)
        assert event.suspension is not None
        assert event.suspension.invocation.invocation_id == request.turn.invocation_id
        assert event.suspension.jobs == (ResourceId(root="job"),)
        assert waits.asked == [request.turn.invocation_id.root]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["up", "down"])
async def test_an_inspection_of_a_resumed_turn_reports_on_the_resume_request(
    transport: str,
) -> None:
    # Core inspects a resumed turn (after a restart, or when its acceptance is unknown)
    # naming the ResumeSessionTurn as the dispatching request, and looks the target up by
    # that identity. The answer must name the same request, as a dispatched turn's does.
    async with world() as w:
        await w.started()
        resume = resume_request()
        assert resume.request_id is not None
        w.host.faults.down = transport == "down"
        resumed = await w.execute(resume)
        w.host.faults.down = False
        inspected = await w.execute(
            inspect_request(
                "req-inspect-resume",
                resume.turn.invocation_id.root,
                dispatch=resume.request_id.root,
            )
        )
        target = inspected.observation.target
        assert target is not None
        assert target.observation.request_id == resume.request_id
        assert_core_accepts([resumed, inspected], expect_retry=False)


@pytest.mark.asyncio
async def test_an_inspection_after_an_upgrade_reads_a_legacy_dispatch_record_as_the_resume() -> (
    None
):
    # Before the fix a resume's dispatch record named the derived "<R>.dispatch" id. A run
    # in flight across the upgrade must still be inspectable by the id core knows.
    async with world() as w:
        await w.started()
        resume = resume_request()
        assert resume.request_id is not None
        await w.execute(resume)
        legacy = 0
        for path in w.base.rglob("*"):
            if path.is_file() and b'"output_schema"' in path.read_bytes():
                text = path.read_text()
                if f'"{resume.request_id.root}"' in text:
                    path.write_text(
                        text.replace(
                            f'"{resume.request_id.root}"', f'"{resume.request_id.root}.dispatch"'
                        )
                    )
                    legacy += 1
        assert legacy == 1, "the dispatch record is on disk exactly once"
        inspected = await w.execute(
            inspect_request(
                "req-inspect-legacy",
                resume.turn.invocation_id.root,
                dispatch=resume.request_id.root,
            )
        )
        target = inspected.observation.target
        assert target is not None
        assert target.observation.request_id == resume.request_id
        assert_core_accepts([inspected], expect_retry=False)


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
async def test_a_resume_refused_before_dispatch_releases_the_turn_and_never_the_session() -> None:
    """The refusal proves this turn holds nothing; the session it would continue stays held.

    So the answer names no resource: core reads a released answer that names the session's
    resource as that session's release.
    """
    async with world() as w:
        await w.execute(ensure_request())
        nothing_to_resume = await w.execute(resume_request("req-a"))
        await w.started()
        assert status(await w.execute(resume_request("req-b"))) is ObservationStatus.SUCCEEDED
        second_successor = await w.execute(resume_request("req-c", invocation="inv-3"))
        for refused in (nothing_to_resume, second_successor):
            observed = refused.observation.observation
            assert observed.status is ObservationStatus.REJECTED
            assert holds_nothing(refused)
            assert observed.resource_id is None, observed.diagnostic


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


@settings(
    max_examples=examples(pr=5, nightly=12),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
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


@pytest.mark.asyncio
async def test_a_turn_queued_behind_an_interrupted_one_proceeds_only_after_cancel_releases_it() -> (
    None
):
    async with world() as w:
        assert status(await w.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        w.host.faults.down = True
        assert status(await w.execute(dispatch_request("req-1", "inv-1"))) is (
            ObservationStatus.UNKNOWN
        )
        w.host.faults.down = False
        blocked = await w.execute(dispatch_request("req-2", "inv-2"))
        assert status(blocked) is ObservationStatus.FAILED  # not run, so core asks again
        assert blocked.observation.observation.retryable
        turns = len(w.host.turns)
        assert status(await w.execute(cancel_request())) is ObservationStatus.CANCELLED
        released = await w.execute(dispatch_request("req-2", "inv-2"))
        assert status(released) is ObservationStatus.SUCCEEDED
        assert len(w.host.turns) == turns + 1


@pytest.mark.asyncio
async def test_run_snapshot_proof_accepts_a_released_turn_but_not_a_recovered_one() -> None:
    gate = TurnGate()
    async with world(gate) as w:
        assert status(await w.execute(ensure_request())) is ObservationStatus.SUCCEEDED
        sessions = FakeAgentSessions(w.host.client, w.host.journal, threads=OsThreads())
        proof = ReleasedRunInvocations(
            JournalRunInvocations(sessions, w.store()), sessions, w.store()
        )
        snapshot = run_snapshot_request()
        assert proof.unproven(snapshot) is not None, "never dispatched"
        running = asyncio.create_task(w.execute(dispatch_request()))
        await wait_until_started(gate.started, running)
        gate.proceed.set()
        await running
        assert proof.unproven(snapshot) is None, "settled"

    async with world() as w:
        await w.execute(ensure_request())
        w.host.faults.down = True
        await w.execute(dispatch_request())
        sessions = FakeAgentSessions(w.host.client, w.host.journal, threads=OsThreads())
        proof = ReleasedRunInvocations(
            JournalRunInvocations(sessions, w.store()), sessions, w.store()
        )
        assert proof.unproven(run_snapshot_request()) is not None, "acceptance unknown"
        assert status(await w.execute(cancel_request())) is ObservationStatus.CANCELLED
        assert proof.unproven(run_snapshot_request()) is None, "interrupted and released"
