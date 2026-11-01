"""Real inbox requests are answered durably through the typed journal."""

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p
from vibesys.events import CoreEventType, RunStartedData
from vibesys.run import EventJournal
from vibesys.run.core_run import StoredSteerArtifacts
from vs_core.api import RunResultProposal, RunStatus, initial_state, step
from vs_project.api import OrchestrationDescriptor, Project, RequestState, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runlog.api import RunLogReader
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    PauseNowRequest,
    PauseRequest,
    ResumeRequest,
    SetMaxParallelRequest,
    SteerRequest,
    WithdrawRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads

_REQUESTS = [
    PauseRequest(control_id="pause"),
    ResumeRequest(control_id="resume"),
    SetMaxParallelRequest(control_id="parallel", max_parallel=2),
    SteerRequest(control_id="steer", text="try a different approach"),
    WithdrawRequest(control_id="withdraw", withdraws="missing"),
    CancelAttemptRequest(control_id="cancel-attempt", attempt="missing"),
    CancelItemRequest(control_id="cancel-item", item="missing"),
    PauseNowRequest(control_id="stop"),
]


@pytest.mark.parametrize(
    "actions",
    [list(range(len(_REQUESTS) // 2)), list(range(len(_REQUESTS) // 2, len(_REQUESTS)))],
    ids=["first-half", "second-half"],
)
def test_all_inbox_actions_have_one_outcome_across_relaunch(
    tmp_path: Path, actions: list[int]
) -> None:
    """Every kind of request once, a crash before each acknowledgement, in two halves.

    A relaunch reads the whole log back, so the cost of a run of requests grows faster than
    their number: two halves cover every kind for about half the cost of the whole list.
    """
    assert_outcomes(tmp_path, RequestRun(actions, crash_before_ack=True))


def test_outcomes_follow_posting_order_for_a_batch_of_applied_and_refused_requests(
    tmp_path: Path,
) -> None:
    assert_outcomes(tmp_path, RequestRun([0, 4, 1, 5, 2, 6], post_all=True))


# Ids whose string order differs from any posting order a test sends them in: ``request-10``
# sorts before ``request-2``, and a capital before a lower-case letter.
_ID_POOL = ["request-10", "request-9", "request-2", "request-1", "B", "a", "a.b", "0", "Zz", "k_1"]


@given(
    actions=st.lists(st.integers(min_value=0, max_value=7), min_size=1, max_size=6),
    ids=st.permutations(_ID_POOL),
    arrival=st.sampled_from(["one-by-one", "one-by-one-crashing-before-the-marker", "as-a-batch"]),
)
def test_outcomes_are_idempotent_over_request_sequences(
    actions: list[int], ids: list[str], arrival: str
) -> None:
    """Any requests under any ids, a relaunch between each: one outcome each, in posting order.

    A relaunch reads the whole log back, so the cost of a sequence grows faster than its length;
    ids drawn from names whose string order differs from the posting order put what a longer
    sequence of ``request-N`` would (an id order taken for the posting order) in two requests,
    and a batch is the arrival that has an order to keep.
    """
    run = RequestRun(
        actions,
        crash_before_ack=arrival == "one-by-one-crashing-before-the-marker",
        post_all=arrival == "as-a-batch",
        ids=ids[: len(actions)],
    )
    with TemporaryDirectory() as directory:
        assert_outcomes(Path(directory), run)


def test_outcome_recovery_uses_typed_records_after_a_crash_before_mirroring(tmp_path: Path) -> None:
    assert_outcomes(tmp_path, RequestRun([0, 1, 2, 3], crash_before_ack=True, mirror_gap=True))


@pytest.mark.parametrize("terminal", [False, True])
def test_a_committed_refusal_is_not_reapplied_after_a_crash_before_its_inbox_marker(
    tmp_path: Path, *, terminal: bool
) -> None:
    project = inbox_project(tmp_path)
    run_id = "run-outcomes"
    inbox = project.state.control_inbox(run_id)
    request = PauseRequest(control_id="pause")
    inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
    clock = ManualClock(start=100)
    events = open_journal(project, run_id, clock)
    reason = "the prior run ended before applying this request"
    events.intent_outcome(request, reason)
    events.close()
    events = open_journal(project, run_id, clock)
    controls = inbox_bridge(project, run_id, events)
    core = initial_state()
    if terminal:
        core = core.model_copy(
            update={
                "run": core.run.model_copy(
                    update={
                        "status": RunStatus.TERMINAL,
                        "result": RunResultProposal(outcome="cancelled", reason="stopped: request"),
                    }
                )
            }
        )
        controls.end(core)
    else:
        assert controls.poll(core, clock.now()) == ()
    status = inbox.status(request.control_id, ended=None)
    assert status is not None
    assert status.state is RequestState.REJECTED
    assert status.reason == reason
    events.close()
    assert_recorded_outcomes(project, run_id, [request.control_id])


@dataclass(frozen=True)
class RequestRun:
    """Requests sent one after another, and what happens to the process around them."""

    actions: list[int]
    crash_before_ack: bool = False
    post_all: bool = False
    mirror_gap: bool = False
    ids: list[str] | None = None
    """The control id of each request; ``request-N`` when absent."""


def assert_outcomes(root: Path, sequence: RequestRun) -> None:
    actions, crash_before_ack = sequence.actions, sequence.crash_before_ack
    post_all, mirror_gap = sequence.post_all, sequence.mirror_gap
    names = sequence.ids or [f"request-{index}" for index in range(len(actions))]
    project = inbox_project(root)
    run_id = "run-outcomes"
    inbox = project.state.control_inbox(run_id)
    clock = ManualClock(start=100)
    core = initial_state()
    journal = partial(open_journal, project, run_id, clock)

    events = journal()
    if post_all:
        for index, action in enumerate(actions):
            request = _REQUESTS[action].model_copy(update={"control_id": names[index]})
            inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
    expected_ids = []
    for index, action in enumerate(actions):
        request = _REQUESTS[action].model_copy(update={"control_id": names[index]})
        expected_ids.append(request.control_id)
        # Posting while the process is down must not consume the request.
        events.close()
        if not post_all:
            inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
        clock.advance(1000)
        events = journal()
        controls = inbox_bridge(project, run_id, events)
        changes = controls.poll(core, clock.now())
        for change in changes:
            core = step(core, change).state
        if crash_before_ack and changes:
            # Durable prefix: core and the outcome committed, inbox marker did not.
            if mirror_gap:
                events.close()
                events = journal(mirror=False)
            events.intent_outcome(request, None)
            events.close()
            clock.advance(1000)
            events = journal()
            controls = inbox_bridge(project, run_id, events)
            for change in controls.poll(core, clock.now()):
                core = step(core, change).state
        controls.settled(core, running=False)
        status = inbox.status(request.control_id, ended=None)
        assert status is not None
        assert status.state in {RequestState.APPLIED, RequestState.REJECTED}
        # Resending a consumed request after another process start adds no outcome.
        pending = inbox.pending()
        inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
        assert inbox.pending() == pending
    events.close()
    assert_recorded_outcomes(project, run_id, expected_ids)


def inbox_project(root: Path) -> Project:
    project = Project.open(root)
    run_id = "run-outcomes"
    at = datetime(2026, 1, 1, tzinfo=UTC)
    project.state.create_project("inbox", now=at)
    project.state.create_run(
        project.state.new_run_manifest(
            "inbox",
            run_id=run_id,
            branch="test",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
            trusted_input_baseline="a" * 40,
            now=at,
        )
    )
    return project


def inbox_bridge(project: Project, run_id: str, events: EventJournal) -> RunControlBridge:
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads)
    control = ChannelRunControl(channel, threads)
    texts = StoredSteerArtifacts(ArtifactStore(project.state.local_namespace(run_id, "core")))
    return RunControlBridge(
        channel,
        texts,
        OperatorInbox(project.state.control_inbox(run_id), control, events),
    )


def open_journal(
    project: Project, run_id: str, clock: ManualClock, *, mirror: bool = True
) -> EventJournal:
    result = EventJournal(clock, SimThreads())
    if mirror:
        result.enable_runlog(project, run_id)
    result.attach(Project.log_directory_for(project.root, run_id), run_id)
    result.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    return result


def assert_recorded_outcomes(project: Project, run_id: str, expected_ids: list[str]) -> None:
    with Project.run_log_path_for(project.root, run_id).open("rb") as stream:
        records = list(RunLogReader(stream))
    outcomes = [r.body.value for r in records if r.body and r.body.field == "intent"]
    assert [outcome.control_id for outcome in outcomes] == expected_ids
    for outcome in outcomes:
        p.validate(outcome)
        if outcome.step == p.IntentStep.REFUSED:
            assert outcome.reason
