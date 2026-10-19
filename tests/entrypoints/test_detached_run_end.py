"""A detached server's record says who paused its run, and its tombstone says how the run ended."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from entrypoints.detached_run import SERVER_EXITED, RecordPublisher
from server.host_api import (
    END_REASON_MAX_CHARACTERS,
    EndRecord,
    FailureReason,
    InstanceStatus,
    LiveInstanceRecord,
    PauseCause,
    RunEndingFact,
    RunEndOutcome,
    RunPhase,
    RunStatus,
)
from server.registry import FakeInstanceMetadataStore, FakeInstanceStore, LiveRegistry
from vs_sim.api.testing import ManualClock, SimThreads, examples

INSTANCE = "0123456789ab"
_FAILURE = FailureReason(code="run_failed", stage="run", message="Agent turn failed\nTraceback")
_CORE_OUTCOMES = [
    outcome
    for outcome in RunEndOutcome
    if outcome not in {RunEndOutcome.FAILED, RunEndOutcome.FORCE_STOPPED}
]


def _running_record() -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=INSTANCE,
        status=InstanceStatus.SERVING,
        phase=RunPhase(status=RunStatus.RUNNING),
        socket_path="/run/s.sock",
        log_path="/run/server.log",
        project_root="/project",
        run_id="run-1",
        pid=4242,
        started_at=1.0,
        hostname="node",
        vibesys_version="0+test",
    )


@dataclass(frozen=True)
class _Status:
    status: RunStatus


@dataclass(frozen=True)
class _Failed:
    pass


@dataclass(frozen=True)
class _QuotaPause:
    pass


@dataclass(frozen=True)
class _Ending:
    fact: RunEndingFact


@dataclass(frozen=True)
class _Terminated:
    pass


type _Fact = _Status | _Failed | _QuotaPause | _Ending | _Terminated


def _tell(publisher: RecordPublisher, fact: _Fact) -> None:
    match fact:
        case _Status(status=status):
            publisher.observe(status=status)
        case _Failed():
            publisher.observe(failure=_FAILURE)
        case _QuotaPause():
            publisher.pausing_for(PauseCause.QUOTA)
        case _Ending(fact=ending):
            publisher.ending(ending)
        case _Terminated():
            publisher.terminated()


def _serve(facts: list[_Fact]) -> tuple[list[LiveInstanceRecord], LiveInstanceRecord]:
    """The record after each fact, and the tombstone the server left."""
    threads = SimThreads()
    store = FakeInstanceStore()
    registry = LiveRegistry(store, ManualClock(100.0))

    def scenario() -> list[LiveInstanceRecord]:
        seen = []
        with registry.register(INSTANCE) as hold:
            publisher = RecordPublisher(
                hold,
                _running_record(),
                ManualClock(100.0),
                metadata=FakeInstanceMetadataStore(),
                threads=threads,
            )
            for fact in facts:
                _tell(publisher, fact)
                seen.append(_current(registry))
            publisher.end()
        return seen

    seen = threads.run(scenario)
    (tombstone,) = registry.list().ended
    return seen, tombstone


def _current(registry: LiveRegistry) -> LiveInstanceRecord:
    (record,) = registry.read().instances
    return record


_ENDINGS = st.builds(
    RunEndingFact,
    outcome=st.none() | st.sampled_from(_CORE_OUTCOMES),
    summary=st.sampled_from(["", "adopted: candidate c-3 beat the input by 12%", "x" * 300]),
    resumable=st.booleans(),
    rounds_finished=st.integers(0, 40),
)
_FACTS: st.SearchStrategy[_Fact] = st.one_of(
    st.builds(
        _Status,
        st.sampled_from(
            [
                RunStatus.RUNNING,
                RunStatus.PAUSING,
                RunStatus.PAUSED,
                RunStatus.STOPPING,
                RunStatus.STOPPED,
                RunStatus.COMPLETED,
            ]
        ),
    ),
    st.just(_Failed()),
    st.just(_QuotaPause()),
    st.builds(_Ending, _ENDINGS),
    st.just(_Terminated()),
)


@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
@given(facts=st.lists(_FACTS, max_size=12))
def test_any_sequence_of_facts_yields_a_consistent_record_and_tombstone(facts: list[_Fact]) -> None:
    seen, tombstone = _serve(facts)

    phase = RunPhase(status=RunStatus.RUNNING)
    cause: PauseCause | None = None
    ending: RunEndingFact | None = None
    terminated = False
    for fact, record in zip(facts, seen, strict=True):
        before = phase.status
        match fact:
            case _Status(status=status):
                phase = phase.advance(status=status)
            case _Failed():
                phase = phase.advance(failure=_FAILURE)
            case _QuotaPause():
                cause = PauseCause.QUOTA
            case _Ending(fact=reported):
                ending = reported
            case _Terminated():
                terminated = True
        pausing = {RunStatus.PAUSING, RunStatus.PAUSED}
        if before in pausing and phase.status not in pausing:
            cause = None
        assert record.phase == phase
        expected = (cause or PauseCause.USER) if phase.status is RunStatus.PAUSED else None
        assert record.paused == expected

    phase = phase.advance(failure=SERVER_EXITED)
    assert tombstone.phase == phase
    assert tombstone.paused is None
    assert _shape(tombstone.end) == _expected_end(phase, ending, terminated=terminated)


def _expected_end(
    phase: RunPhase, ending: RunEndingFact | None, *, terminated: bool
) -> tuple[RunEndOutcome, bool, int | None] | None:
    """The tombstone's outcome, resumability and resume round; ``None`` when it has no end."""
    if terminated:
        outcome = RunEndOutcome.FORCE_STOPPED
    elif ending is not None and ending.outcome is not None:
        outcome = ending.outcome
    elif phase.status is RunStatus.STOPPED:
        outcome = RunEndOutcome.STOPPED
    elif phase.status is RunStatus.FAILED:
        outcome = RunEndOutcome.FAILED
    else:
        return None
    resumable = (
        ending is not None and ending.resumable and outcome is not RunEndOutcome.FORCE_STOPPED
    )
    rounds = None if ending is None or not resumable else ending.rounds_finished + 1
    return outcome, resumable, rounds


def _shape(end: EndRecord | None) -> tuple[RunEndOutcome, bool, int | None] | None:
    if end is None:
        return None
    assert 0 < len(end.reason) <= END_REASON_MAX_CHARACTERS
    assert "\n" not in end.reason
    return end.outcome, end.resumable, end.resume_from_round


@pytest.mark.parametrize("outcome", _CORE_OUTCOMES)
def test_a_run_that_recorded_a_result_ends_with_cores_reason(outcome: RunEndOutcome) -> None:
    ending = RunEndingFact(
        outcome=outcome,
        summary=f"{outcome}: the search said why",
        resumable=False,
        rounds_finished=3,
    )
    status = RunStatus.STOPPED if outcome is RunEndOutcome.STOPPED else RunStatus.COMPLETED

    _, tombstone = _serve([_Ending(ending), _Status(status)])

    assert tombstone.end == EndRecord(
        outcome=outcome, reason=f"{outcome}: the search said why", resumable=False
    )


def test_a_failed_run_ends_failed_with_the_first_line_of_its_failure() -> None:
    ending = RunEndingFact(outcome=None, resumable=True, rounds_finished=4)

    _, tombstone = _serve([_Ending(ending), _Status(RunStatus.FAILED), _Failed()])

    assert tombstone.end == EndRecord(
        outcome=RunEndOutcome.FAILED,
        reason="Agent turn failed",
        resumable=True,
        resume_from_round=5,
    )


def test_a_terminated_server_ends_force_stopped_for_good() -> None:
    """Regression: a force-stopped run was offered a resume, while a cleanly stopped one was not."""
    ending = RunEndingFact(outcome=None, resumable=True, rounds_finished=0)

    _, tombstone = _serve([_Terminated(), _Ending(ending), _Failed()])

    assert tombstone.end is not None
    assert tombstone.end.outcome is RunEndOutcome.FORCE_STOPPED
    assert (tombstone.end.resumable, tombstone.end.resume_from_round) == (False, None)


def test_a_server_that_exits_before_its_run_ends_leaves_a_failed_end_that_cannot_resume() -> None:
    _, tombstone = _serve([])

    assert tombstone.end == EndRecord(
        outcome=RunEndOutcome.FAILED, reason=SERVER_EXITED.message, resumable=False
    )


def test_a_stop_whose_ending_was_not_committed_can_be_lifted_by_a_resume() -> None:
    """A stop is a pause the operator lifts: the run's record is not terminal, so Resume is offered."""
    ending = RunEndingFact(outcome=None, resumable=True, rounds_finished=2)

    _, tombstone = _serve(
        [_Status(RunStatus.STOPPING), _Ending(ending), _Status(RunStatus.STOPPED)]
    )

    assert tombstone.end == EndRecord(
        outcome=RunEndOutcome.STOPPED,
        reason="Stopped by request.",
        resumable=True,
        resume_from_round=3,
    )


def test_a_stop_whose_ending_committed_ends_for_good() -> None:
    ending = RunEndingFact(outcome=None, resumable=False, rounds_finished=2)

    _, tombstone = _serve(
        [_Status(RunStatus.STOPPING), _Ending(ending), _Status(RunStatus.STOPPED)]
    )

    assert tombstone.end == EndRecord(
        outcome=RunEndOutcome.STOPPED, reason="Stopped by request.", resumable=False
    )


def test_a_quota_pause_is_reported_until_the_run_resumes() -> None:
    seen, _ = _serve(
        [
            _QuotaPause(),
            _Status(RunStatus.PAUSING),
            _Status(RunStatus.PAUSED),
            _Status(RunStatus.RUNNING),
            _Status(RunStatus.PAUSING),
            _Status(RunStatus.PAUSED),
        ]
    )

    assert [record.paused for record in seen] == [
        None,
        None,
        PauseCause.QUOTA,
        None,
        None,
        PauseCause.USER,
    ]


@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
@given(facts=st.lists(_FACTS, max_size=12), crashes=st.booleans())
def test_only_a_crash_or_an_open_failure_leaves_a_run_resumable(
    facts: list[_Fact], *, crashes: bool
) -> None:
    """Over any stop, force stop and crash ordering: a stop ends the run; a crash keeps it."""
    threads = SimThreads()
    store = FakeInstanceStore()
    registry = LiveRegistry(store, ManualClock(100.0))

    def scenario() -> None:
        hold = store.hold(INSTANCE)
        publisher = RecordPublisher(
            hold,
            _running_record(),
            ManualClock(100.0),
            metadata=FakeInstanceMetadataStore(),
            threads=threads,
        )
        for fact in facts:
            _tell(publisher, fact)
        if crashes:
            hold.crash()
        else:
            publisher.end()
            hold.release()

    threads.run(scenario)
    (tombstone,) = registry.list().ended
    end = tombstone.end
    if crashes:
        assert end is not None
        assert end.resumable
        return
    stopped = any(isinstance(fact, _Terminated) for fact in facts) or tombstone.phase.status is (
        RunStatus.STOPPED
    )
    if end is not None and stopped:
        assert not end.resumable
