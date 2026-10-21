"""Real inbox requests are answered durably through the typed journal."""

from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p
from vibesys.events import CoreEventType, RunStartedData
from vibesys.run import EventJournal
from vibesys.run.core_run import StoredSteerArtifacts
from vs_core.api import initial_state, step
from vs_project.api import OrchestrationDescriptor, Project, RequestState, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runlog.api import RunLogReader
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    PauseRequest,
    ResumeRequest,
    SetMaxParallelRequest,
    SteerRequest,
    StopRequest,
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
    StopRequest(control_id="stop"),
]


def test_all_inbox_actions_have_one_outcome_across_relaunch(tmp_path: Path) -> None:
    assert_outcomes(tmp_path, list(range(len(_REQUESTS))), crash_before_ack=True)


def test_outcomes_follow_posting_order_for_a_batch_of_applied_and_refused_requests(
    tmp_path: Path,
) -> None:
    assert_outcomes(tmp_path, [0, 4, 1, 5, 2, 6], crash_before_ack=False, post_all=True)


@given(
    actions=st.lists(st.integers(min_value=0, max_value=7), min_size=1, max_size=12),
    crash_before_ack=st.booleans(),
)
def test_outcomes_are_idempotent_over_request_sequences(
    actions: list[int], *, crash_before_ack: bool
) -> None:
    with TemporaryDirectory() as directory:
        assert_outcomes(Path(directory), actions, crash_before_ack=crash_before_ack)


def test_outcome_recovery_uses_typed_records_after_a_crash_before_mirroring(tmp_path: Path) -> None:
    assert_outcomes(tmp_path, [0, 1, 2, 3], crash_before_ack=True, mirror_gap=True)


def assert_outcomes(
    root: Path,
    actions: list[int],
    *,
    crash_before_ack: bool,
    post_all: bool = False,
    mirror_gap: bool = False,
) -> None:
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
    inbox = project.state.control_inbox(run_id)
    clock = ManualClock(start=100)
    core = initial_state()
    texts = StoredSteerArtifacts(ArtifactStore(project.state.local_namespace(run_id, "core")))

    journal = partial(open_journal, project, run_id, clock)

    def bridge(events: EventJournal) -> RunControlBridge:
        threads = SimThreads()
        channel = create_run_control_channel(FakeRunControlEventSink(), threads)
        control = ChannelRunControl(channel, threads)
        return RunControlBridge(
            channel, texts, OperatorInbox(inbox, control, events.intent_outcome)
        )

    events = journal()
    if post_all:
        for index, action in enumerate(actions):
            request = _REQUESTS[action].model_copy(update={"control_id": f"request-{index}"})
            inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
    expected_ids = []
    for index, action in enumerate(actions):
        request = _REQUESTS[action].model_copy(update={"control_id": f"request-{index}"})
        expected_ids.append(request.control_id)
        # Posting while the process is down must not consume the request.
        events.close()
        if not post_all:
            inbox.post(request.control_id, request.model_dump_json().encode(), ended=lambda: None)
        clock.advance(1000)
        events = journal()
        controls = bridge(events)
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
            controls = bridge(events)
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
