"""HostCall protobuf round trips use the real durable inbox, without a child process."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_proto.api as p
from entrypoints.control import control_main
from entrypoints.host import main
from vibesys.api import CONTROL_REQUEST
from vibesys.api.host import host_call
from vibesys.events import CoreEventType, RunStartedData
from vibesys.run import EventJournal
from vs_core.api import RunResultProposal, RunStatus, initial_state
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_project.api.testing import run_execution_record, seed_stored_record
from vs_proto.api import (
    CancelAttemptIntent,
    CancelItemIntent,
    HostCallRequest,
    HostCallResponse,
    Oneof,
    PauseIntent,
    PauseNowIntent,
    PostIntentRequest,
    RelaunchRefusal,
    RelaunchRequest,
    SetParallelismIntent,
    SteerIntent,
    SteerMode,
    SteerTarget,
    SteerTargetKind,
    WithdrawIntent,
    validate,
)
from vs_runlog.api import (
    FileRunLogWriter,
    RunLock,
    RunLogReader,
    encode_frame,
    epoch_has_ended,
    read_current_epoch,
    writer_identity_path,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakeProcesses,
    FakeProcessSignaller,
    ManualClock,
    ProcessScript,
    Sim,
    SimThreads,
    examples,
)

if TYPE_CHECKING:
    from protobuf import Message

    from vs_sim.api import ProcessSpec


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[Project, str]:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state-home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    project.state.create_project("control", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "control",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    with project.state.exclusive_run_host(manifest.run_id):
        pass
    if getattr(request, "param", True):
        project.state.control_inbox(manifest.run_id).serve()
    return project, manifest.run_id


def inbox_call(payload: bytes) -> bytes:
    threads = SimThreads()
    processes = FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep)
    response = host_call(
        payload,
        platform=replace(
            Sim(seed=0).platform(
                threads, processes=processes, signaller=FakeProcessSignaller(set())
            ),
            writer_signaller=None,
        ),
    )
    assert not processes.ran
    return response


def call(project: Project, run_id: str, intent: Oneof, request_id: str = "r-1") -> HostCallResponse:
    request = HostCallRequest(
        checkout="/src/vibesys",
        method=Oneof(
            "post_intent",
            PostIntentRequest(
                project_root=str(project.root),
                run_id=run_id,
                request_id=request_id,
                intent=intent,
                expected_epoch=1,
            ),
        ),
    )
    response = HostCallResponse.from_binary(inbox_call(request.to_binary()))
    validate(response)
    return response


@pytest.mark.parametrize(
    ("name", "intent", "action"),
    [
        ("pause_now", PauseNowIntent(), "pause_now"),
        ("pause", PauseIntent(), "pause"),
        (
            "steer",
            SteerIntent(
                text="look here",
                target=SteerTarget(kind=SteerTargetKind.RUN),
                mode=SteerMode.NEXT_TURN,
            ),
            "steer",
        ),
        (
            "steer",
            SteerIntent(
                text="look here",
                target=SteerTarget(kind=SteerTargetKind.ROLE, id="planner"),
                mode=SteerMode.STANDING,
            ),
            "steer",
        ),
        ("withdraw", WithdrawIntent(withdraws="earlier"), "withdraw"),
        ("cancel_attempt", CancelAttemptIntent(attempt="A-1"), "cancel_attempt"),
        ("cancel_item", CancelItemIntent(item="W-1"), "cancel_item"),
        ("set_parallelism", SetParallelismIntent(max_parallel=3), "set_max_parallel"),
    ],
)
def test_post_round_trips_into_reviewed_inbox(
    project: tuple[Project, str], name: str, intent: Message, action: str
) -> None:
    owner, run_id = project
    response = call(owner, run_id, Oneof(name, intent))
    assert response.result is not None
    assert response.result.field == "post_intent"
    assert response.result.value.request_id == "r-1"
    assert response.result.value.ordered_reader
    inbox = owner.state.control_inbox(run_id)
    held = inbox.pending()
    assert len(held) == 1
    parsed = CONTROL_REQUEST.validate_python(json.loads(held[0].payload))
    assert parsed.action.value == action
    assert parsed.control_id == "r-1"
    assert call(owner, run_id, Oneof(name, intent)) == response
    assert inbox.pending() == held


def test_order_and_request_id_conflicts_are_unchanged(project: tuple[Project, str]) -> None:
    owner, run_id = project
    call(owner, run_id, Oneof("pause", PauseIntent()), "z")
    call(owner, run_id, Oneof("pause_now", PauseNowIntent()), "a")
    call(owner, run_id, Oneof("pause", PauseIntent()), "z")
    response = call(owner, run_id, Oneof("pause_now", PauseNowIntent()), "z")
    assert response.result is not None
    assert response.result.field == "configuration_failed"
    assert "different request" in response.result.value.message
    assert [entry.request_id for entry in owner.state.control_inbox(run_id).pending()] == ["z", "a"]


@pytest.mark.parametrize(
    "intent",
    [
        Oneof("steer", SteerIntent(text="guidance")),
        Oneof("withdraw", WithdrawIntent(withdraws="../bad")),
        Oneof("set_parallelism", SetParallelismIntent()),
    ],
)
def test_invalid_or_unsupported_intents_never_write(
    project: tuple[Project, str], intent: Oneof
) -> None:
    owner, run_id = project
    response = call(owner, run_id, intent)
    assert response.result is not None
    assert response.result.field == "configuration_failed"
    assert response.result.value.exit_code == 2
    assert not owner.state.control_inbox(run_id).pending()


@given(st.binary(max_size=500))
def test_arbitrary_input_has_a_typed_result(payload: bytes) -> None:
    response = HostCallResponse.from_binary(inbox_call(payload))
    validate(response)
    assert response.result is not None
    assert response.result.field == "configuration_failed"


@pytest.mark.parametrize("project", [False], indirect=True)
def test_no_ordered_reader_is_reported_without_claiming_application(
    project: tuple[Project, str],
) -> None:
    owner, run_id = project
    response = call(owner, run_id, Oneof("pause", PauseIntent()))
    assert response.result is not None
    assert response.result.field == "post_intent"
    assert not response.result.value.ordered_reader
    assert owner.state.control_inbox(run_id).pending()


def test_host_command_uses_binary_stdin_stdout_and_the_real_inbox(
    project: tuple[Project, str],
) -> None:
    owner, run_id = project
    request = HostCallRequest(
        checkout="/src/vibesys",
        method=Oneof(
            "post_intent",
            PostIntentRequest(
                project_root=str(owner.root),
                run_id=run_id,
                request_id="command-1",
                expected_epoch=1,
                intent=Oneof("pause", PauseIntent()),
            ),
        ),
    )
    output = BytesIO()
    assert main(["post-intent"], stdin=BytesIO(request.to_binary()), stdout=output) == 0
    response = HostCallResponse.from_binary(output.getvalue())
    validate(response)
    assert response.result is not None
    assert response.result.field == "post_intent"
    assert owner.state.control_inbox(run_id).pending()[0].request_id == "command-1"


def test_oversized_input_has_a_typed_failure() -> None:
    response = HostCallResponse.from_binary(inbox_call(b"x" * ((1 << 20) + 1)))
    validate(response)
    assert response.result is not None
    assert response.result.field == "configuration_failed"
    assert response.result.value.code == "request_too_large"


def test_ended_run_refuses_new_requests_but_accepts_an_applied_retry(
    project: tuple[Project, str],
) -> None:
    owner, run_id = project
    call(owner, run_id, Oneof("pause", PauseIntent()))
    inbox = owner.state.control_inbox(run_id)
    inbox.settle("r-1")
    core = initial_state()
    ended = core.model_copy(
        update={
            "run": core.run.model_copy(
                update={
                    "status": RunStatus.TERMINAL,
                    "result": RunResultProposal(outcome="cancelled", reason="stopped: by request"),
                }
            )
        }
    )
    seed_stored_record(
        owner,
        run_id,
        StoredEnvelope(
            revision=0,
            schema_version=1,
            payload=json.dumps({"envelope": {"core": ended.model_dump(mode="json")}}).encode(),
        ),
    )
    accepted = call(owner, run_id, Oneof("pause", PauseIntent()))
    assert accepted.result is not None
    assert accepted.result.field == "post_intent"
    refused = call(owner, run_id, Oneof("pause_now", PauseNowIntent()), "new")
    assert refused.result is not None
    assert refused.result.field == "configuration_failed"
    assert refused.result.value.code == "inbox_closed"
    assert refused.result.value.exit_code == 3
    assert inbox.status("new", ended="stopped") is None


@pytest.mark.parametrize("cli_first", [True, False])
@pytest.mark.parametrize(
    ("arguments", "intent"),
    [
        (["pause"], Oneof("pause", PauseIntent())),
        (["pause-now"], Oneof("pause_now", PauseNowIntent())),
        (
            ["steer", "look here", "--role", "planner", "--standing"],
            Oneof(
                "steer",
                SteerIntent(
                    text="look here",
                    target=SteerTarget(kind=SteerTargetKind.ROLE, id="planner"),
                    mode=SteerMode.STANDING,
                ),
            ),
        ),
    ],
)
def test_legacy_cli_and_host_call_share_byte_identical_request_ids(
    project: tuple[Project, str], arguments: list[str], intent: Oneof, *, cli_first: bool
) -> None:
    owner, run_id = project
    _store_observed_epoch(owner, run_id)

    def cli() -> None:
        assert (
            control_main(
                [run_id, "--control-id", "shared", *arguments],
                cwd=owner.root,
                new_control_id=lambda: "unused",
                out=StringIO(),
                err=StringIO(),
            )
            == 0
        )

    if cli_first:
        cli()
    response = call(owner, run_id, intent, "shared")
    assert response.result is not None
    assert response.result.field == "post_intent"
    if not cli_first:
        cli()
    assert len(owner.state.control_inbox(run_id).pending()) == 1


def host_executable(checkout: Path) -> Path:
    executable = checkout / ".venv" / "bin" / "vibesys"
    executable.parent.mkdir(parents=True)
    executable.touch()
    return executable


def relaunch_call(owner: Project, run_id: str, checkout: Path) -> HostCallRequest:

    return HostCallRequest(
        checkout=str(checkout),
        method=Oneof(
            "relaunch",
            RelaunchRequest(project_root=str(owner.root), run_id=run_id, expected_epoch=1),
        ),
    )


def _store_observed_epoch(owner: Project, run_id: str) -> None:
    journal = EventJournal(ManualClock(), SimThreads())
    journal.enable_runlog(owner, run_id)
    journal.attach(Project.log_directory_for(owner.root, run_id), run_id)
    try:
        journal.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input")
        )
    finally:
        journal.close()
    core = initial_state()
    core = core.model_copy(
        update={
            "records": core.records.model_copy(
                update={"recovery": core.records.recovery.model_copy(update={"epoch": 1})}
            )
        }
    )
    seed_stored_record(
        owner,
        run_id,
        StoredEnvelope(
            revision=0,
            schema_version=1,
            payload=json.dumps({"envelope": {"core": core.model_dump(mode="json")}}).encode(),
        ),
    )


def _store_ending(owner: Project, run_id: str, ending: str) -> None:
    if ending not in {"interrupted", "stopped"}:
        core = initial_state()
        ended = core.model_copy(
            update={
                "run": core.run.model_copy(
                    update={
                        "status": RunStatus.TERMINAL,
                        "result": RunResultProposal.model_validate(
                            {
                                "outcome": ending
                                if ending in {"success", "failure", "blocked"}
                                else "cancelled",
                                "reason": ending,
                            }
                        ),
                    }
                )
            }
        )
        seed_stored_record(
            owner,
            run_id,
            StoredEnvelope(
                revision=0,
                schema_version=1,
                payload=json.dumps({"envelope": {"core": ended.model_dump(mode="json")}}).encode(),
            ),
        )


@pytest.mark.parametrize(
    "ending", ["interrupted", "stopped", "success", "failure", "blocked", "deadline", "environment"]
)
def test_host_relaunch_round_trip_confirms_the_new_epoch(
    project: tuple[Project, str], tmp_path: Path, ending: str, sim: Sim
) -> None:

    owner, run_id = project
    executable = host_executable(tmp_path / "checkout")
    threads, clock = sim.threads(), sim.wall_clock
    journal = EventJournal(clock, threads)
    journal.checkout = ("tree-a", False)
    journal.enable_runlog(owner, run_id)
    journal.attach(Project.log_directory_for(owner.root, run_id), run_id)
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    if ending == "stopped":
        journal.emit(CoreEventType.STOPPED)
    journal.close()
    _store_ending(owner, run_id, ending)
    path = Project.run_log_path_for(owner.root, run_id)
    request = relaunch_call(owner, run_id, executable.parents[2])

    def launch(spec: ProcessSpec) -> ProcessScript:
        if len(processes.ran) > 1:
            return ProcessScript(returncode=1)
        concurrent = HostCallResponse.from_binary(
            host_call(
                request.to_binary(),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None,
                ),
            )
        )
        assert concurrent.result is not None
        assert concurrent.result.field == "relaunch"
        assert concurrent.result.value.outcome == Oneof("refusal", RelaunchRefusal.LOCKED)
        assert spec.argv == (
            str(executable),
            "--resume",
            run_id,
            "--detach",
            "--project",
            str(owner.root),
        )
        resumed = EventJournal(clock, threads)
        resumed.checkout = ("tree-b", True)
        resumed.enable_runlog(owner, run_id)
        resumed.attach(Project.log_directory_for(owner.root, run_id), run_id)
        resumed.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input")
        )
        resumed.close()
        return ProcessScript()

    processes = FakeProcesses(launch, pass_time=threads.sleep)
    with FileRunLogWriter(path, clock=clock, threads=threads):
        response = HostCallResponse.from_binary(
            host_call(
                request.to_binary(),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None,
                ),
            )
        )
        assert response.result is not None
        assert response.result.field == "relaunch"
        assert response.result.value.outcome == Oneof("refusal", RelaunchRefusal.LOCKED)
        assert not processes.ran
    output = BytesIO()
    assert (
        main(
            ["relaunch"],
            stdin=BytesIO(request.to_binary()),
            stdout=output,
            platform=sim.platform(
                threads, processes=processes, signaller=FakeProcessSignaller(set())
            ),
        )
        == 0
    )
    response = HostCallResponse.from_binary(output.getvalue())
    validate(response)
    assert response.result is not None
    assert response.result.field == "relaunch"
    assert response.result.value.outcome == Oneof("epoch", 2)
    with path.open("rb") as stream:
        records = list(RunLogReader(stream))
    headers = [r.body.value for r in records if r.body and r.body.field == "epoch_started"]
    assert [(h.epoch, h.tree_hash, h.dirty) for h in headers] == [
        (1, "tree-a", False),
        (1, "tree-a", False),
        (2, "tree-b", True),
        (2, "tree-b", True),
    ]
    response = HostCallResponse.from_binary(
        host_call(
            request.to_binary(),
            platform=replace(
                Sim(seed=0).platform(
                    threads, processes=processes, signaller=FakeProcessSignaller(set())
                ),
                writer_signaller=None,
            ),
        )
    )
    assert response.result is not None
    assert response.result.field == "relaunch"
    assert response.result.value.outcome == Oneof("refusal", RelaunchRefusal.EPOCH_CHANGED)
    assert len(processes.ran) == 1


def test_host_relaunch_missing_checkout_refuses_without_spawning(
    project: tuple[Project, str], tmp_path: Path
) -> None:

    owner, run_id = project
    processes = FakeProcesses(lambda _spec: ProcessScript(), pass_time=lambda _seconds: None)
    response = HostCallResponse.from_binary(
        host_call(
            relaunch_call(owner, run_id, tmp_path / "absent").to_binary(),
            platform=replace(
                Sim(seed=0).platform(
                    SimThreads(), processes=processes, signaller=FakeProcessSignaller(set())
                ),
                writer_signaller=None,
            ),
        )
    )
    validate(response)
    assert response.result is not None
    assert response.result.field == "relaunch"
    assert response.result.value.outcome == Oneof("refusal", RelaunchRefusal.CHECKOUT_MISSING)
    assert not processes.ran


@pytest.mark.parametrize("mode", ["ended", "nonzero", "unconfirmed", "corrupt", "missing-log"])
def test_host_relaunch_failure_paths_never_claim_an_epoch(
    project: tuple[Project, str], tmp_path: Path, mode: str
) -> None:
    owner, run_id = project
    executable = tmp_path / "checkout" / ".venv" / "bin" / "vibesys"
    executable.parent.mkdir(parents=True)
    executable.touch()
    path = Project.run_log_path_for(owner.root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        p.RUN_LOG_MAGIC
        + encode_frame(
            p.Record(
                seq=1,
                epoch=1,
                at=p.Timestamp(seconds=1),
                body=p.Oneof("epoch_started", p.EpochStarted(epoch=1, run_id=run_id)),
            )
        )
    )
    if mode == "corrupt":
        path.write_bytes(b"invalid magic")
    elif mode == "missing-log":
        path.unlink()
    threads = SimThreads()
    processes = FakeProcesses(
        lambda _spec: ProcessScript(returncode=1 if mode == "nonzero" else 0),
        pass_time=threads.sleep,
    )
    request = relaunch_call(owner, run_id, executable.parents[2])
    response = threads.run(
        lambda: HostCallResponse.from_binary(
            host_call(
                request.to_binary(),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None,
                ),
            )
        )
    )
    validate(response)
    assert response.result is not None
    if mode == "missing-log":
        assert response.result.field == "relaunch"
        assert response.result.value.outcome == Oneof("refusal", RelaunchRefusal.EPOCH_CHANGED)
        assert not processes.ran
    else:
        assert response.result.field == "configuration_failed"
        assert response.result.value.code == "invalid_input"
        assert len(processes.ran) == (0 if mode == "corrupt" else 1)


def _open_resumable(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Project, str]:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(root / "state-home"))
    project_root = root / "project"
    project_root.mkdir(parents=True)
    project = Project.open(project_root)
    project.state.create_project("control", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "control",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    with project.state.exclusive_run_host(manifest.run_id):
        pass
    project.state.control_inbox(manifest.run_id).serve()
    return project, manifest.run_id


def _resume_during_environment_open(root: Path, *, delay: float, fail: bool) -> None:
    """Resume while the successor holds the writer lock and its environment is still opening."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        _resume_during_environment_open_in(root, monkeypatch, delay=delay, fail=fail)


def _resume_during_environment_open_in(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, delay: float, fail: bool
) -> None:
    owner, run_id = _open_resumable(root, monkeypatch)
    executable = host_executable(root / "checkout")
    threads, clock = SimThreads(), ManualClock()
    logs = Project.log_directory_for(owner.root, run_id)
    path = Project.run_log_path_for(owner.root, run_id)
    request = relaunch_call(owner, run_id, executable.parents[2])

    def exercise() -> None:
        journal = EventJournal(clock, threads)
        journal.checkout = ("tree-a", False)
        journal.enable_runlog(owner, run_id)
        journal.attach(logs, run_id)
        journal.emit(
            CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input")
        )
        journal.close()

        def launch(spec: ProcessSpec) -> ProcessScript:
            def successor() -> None:
                resumed = EventJournal(clock, threads)
                resumed.checkout = ("tree-b", True)
                resumed.enable_runlog(owner, run_id)
                resumed.attach(logs, run_id)
                threads.sleep(delay)
                if fail:
                    resumed.fail_open_epoch("environment did not open")
                else:
                    resumed.emit(
                        CoreEventType.RUN_STARTED,
                        data=RunStartedData(outer_loop="test", input="input"),
                    )
                resumed.close()

            threads.spawn(successor, name="successor", daemon=False)
            assert spec.argv == (
                str(executable),
                "--resume",
                run_id,
                "--detach",
                "--project",
                str(owner.root),
            )
            return ProcessScript()

        processes = FakeProcesses(launch, pass_time=threads.sleep)
        response = HostCallResponse.from_binary(
            host_call(
                request.to_binary(),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None,
                ),
            )
        )
        validate(response)
        assert response.result is not None
        assert response.result.field == "relaunch", response.result
        assert response.result.value.outcome == Oneof("epoch", 2)
        second = HostCallResponse.from_binary(
            host_call(
                request.to_binary(),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None,
                ),
            )
        )
        validate(second)
        assert second.result is not None
        assert second.result.field == "relaunch"
        assert second.result.value.outcome in (
            Oneof("refusal", RelaunchRefusal.LOCKED),
            Oneof("refusal", RelaunchRefusal.EPOCH_CHANGED),
        )
        assert len(processes.ran) == 1

    threads.run(exercise)
    _assert_successor_outcome(path, failed=fail)


def _assert_successor_outcome(path: Path, *, failed: bool) -> None:
    with path.open("rb") as stream:
        records = list(RunLogReader(stream))
    lifecycles = [
        (record.epoch, record.body.value.state.field, record.body.value.state.value)
        for record in records
        if record.body is not None
        and record.body.field == "lifecycle"
        and record.body.value.state is not None
    ]
    if failed:
        ended = [value for epoch, field, value in lifecycles if epoch == 2 and field == "ended"]
        assert len(ended) == 1
        ending = ended[0]
        assert isinstance(ending, p.Ended)
        assert ending.outcome is p.Outcome.FAILED
        assert ending.failure is not None
        assert ending.failure.kind is p.RunFailureKind.ENVIRONMENT_UNAVAILABLE
        return
    assert any(epoch == 2 and field == "running" for epoch, field, _value in lifecycles)


def test_resume_confirms_while_the_environment_opens_slowly(tmp_path: Path) -> None:
    """The reported case: environment open outlasts the confirmation budget."""
    _resume_during_environment_open(tmp_path / "slow", delay=30, fail=False)


@pytest.mark.regression
@settings(max_examples=examples(pr=6, nightly=24))
@given(delay=st.integers(min_value=0, max_value=40), fail=st.booleans())
@example(delay=30, fail=False)
@example(delay=30, fail=True)
def test_resume_confirms_or_records_failure_for_any_environment_open(
    delay: int, *, fail: bool
) -> None:
    """A successor either confirms and runs, or ends the epoch it already published."""
    with TemporaryDirectory() as directory:
        _resume_during_environment_open(Path(directory), delay=delay, fail=fail)


def native_journal(project: tuple[Project, str], sim: Sim) -> tuple[EventJournal, SimThreads]:
    owner, run_id = project
    threads = sim.threads()
    journal = EventJournal(sim.wall_clock, threads)
    journal.checkout = ("tree-a", False)
    journal.enable_runlog(owner, run_id)
    journal.attach(Project.log_directory_for(owner.root, run_id), run_id)
    journal.emit(CoreEventType.RUN_STARTED, data=RunStartedData(outer_loop="test", input="input"))
    return journal, threads


def pause_payload(project: tuple[Project, str], epoch: int = 1) -> bytes:
    owner, run_id = project
    return HostCallRequest(
        checkout="/src/vibesys",
        method=Oneof(
            "post_intent",
            PostIntentRequest(
                project_root=str(owner.root),
                run_id=run_id,
                request_id="pause-1",
                expected_epoch=epoch,
                intent=Oneof("pause_now", PauseNowIntent()),
            ),
        ),
    ).to_binary()


@pytest.mark.parametrize("mode", ["held", "unsupported", "absent", "unknown", "changed", "foreign"])
@pytest.mark.parametrize("head_epoch", [0, 1])
def test_pause_now_keeps_process_outcomes_distinct_without_ending_the_run(
    project: tuple[Project, str], sim: Sim, mode: str, head_epoch: int
) -> None:
    owner, run_id = project
    journal, threads = native_journal(project, sim)
    head = p.RunHead.from_binary(Project.run_head_path_for(owner.root, run_id).read_bytes())
    assert head.process is not None
    head.epoch = head_epoch
    Project.run_head_path_for(owner.root, run_id).write_bytes(head.to_binary())
    signaller = FakeProcessSignaller(
        set() if mode == "absent" else {head.process.pid},
        held_locks={
            head.process.pid: {
                writer_identity_path(Project.run_log_path_for(owner.root, run_id), head.process)
            }
        }
        if mode != "foreign"
        else {},
    )
    if mode == "unknown":
        assert head.process.started_at is not None
        head.process.started_at.seconds += 1
        Project.run_head_path_for(owner.root, run_id).write_bytes(head.to_binary())
    before = threads.now()
    processes = FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep)
    try:
        response = HostCallResponse.from_binary(
            host_call(
                pause_payload(project, 2 if mode == "changed" else 1),
                platform=replace(
                    Sim(seed=0).platform(
                        threads, processes=processes, signaller=FakeProcessSignaller(set())
                    ),
                    writer_signaller=None if mode == "unsupported" else signaller,
                ),
            )
        )
        validate(response)
        assert response.result is not None
        assert response.result.field == "post_intent"
        result = response.result.value.termination
        assert result is not None
        assert (
            result.outcome
            is {
                "held": p.host_payloads.TerminateRunOutcome.REQUESTED_UNCONFIRMED,
                "unsupported": p.host_payloads.TerminateRunOutcome.UNSUPPORTED,
                "absent": p.host_payloads.TerminateRunOutcome.PROCESS_ABSENT,
                "unknown": p.host_payloads.TerminateRunOutcome.OBSERVATION_UNKNOWN,
                "changed": p.host_payloads.TerminateRunOutcome.IDENTITY_CHANGED,
                "foreign": p.host_payloads.TerminateRunOutcome.IDENTITY_CHANGED,
            }[mode]
        )
        assert signaller.terminated == ([head.process.pid] if mode == "held" else [])
        assert owner.state.control_inbox(run_id).pending()[0].request_id == "pause-1"
        if mode == "held":
            assert threads.now() - before == pytest.approx(9.75)
        if mode != "unknown":
            current = read_current_epoch(Project.run_log_path_for(owner.root, run_id))
            assert current is not None
            assert not epoch_has_ended(current)
        assert not processes.ran
    finally:
        journal.close()


@pytest.mark.parametrize("release_at", [1, 6])
def test_pause_now_gives_the_owner_cleanup_grace_and_observes_release(
    project: tuple[Project, str], sim: Sim, release_at: int
) -> None:
    owner, run_id = project
    journal, threads = native_journal(project, sim)
    head = p.RunHead.from_binary(Project.run_head_path_for(owner.root, run_id).read_bytes())
    assert head.process is not None
    signaller = FakeProcessSignaller(
        {head.process.pid},
        held_locks={
            head.process.pid: {
                writer_identity_path(Project.run_log_path_for(owner.root, run_id), head.process)
            }
        },
    )

    def release() -> None:
        threads.sleep(release_at)
        journal.close()

    worker = threads.spawn(release, name="writer-cleanup")
    try:
        response = HostCallResponse.from_binary(
            host_call(
                pause_payload(project),
                platform=replace(
                    Sim(seed=0).platform(
                        threads,
                        processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
                        signaller=FakeProcessSignaller(set()),
                    ),
                    writer_signaller=signaller,
                ),
            )
        )
        assert response.result is not None
        assert response.result.field == "post_intent"
        result = response.result.value.termination
        assert result is not None
        assert result.outcome is p.host_payloads.TerminateRunOutcome.WRITER_RELEASED
        assert signaller.terminated == ([] if release_at == 1 else [head.process.pid])
    finally:
        worker.join(HANG_GUARD_S)
        journal.close()


def test_pause_now_refuses_a_reused_pid_while_a_successor_holds_the_run_lock(
    project: tuple[Project, str], sim: Sim
) -> None:
    owner, run_id = project
    journal, threads = native_journal(project, sim)
    head = p.RunHead.from_binary(Project.run_head_path_for(owner.root, run_id).read_bytes())
    assert head.process is not None
    journal.close()
    successor = RunLock(Project.run_log_path_for(owner.root, run_id).parent / "run.lock")
    signaller = FakeProcessSignaller(
        {head.process.pid},
        held_locks={
            head.process.pid: {
                writer_identity_path(Project.run_log_path_for(owner.root, run_id), head.process)
            }
        },
    )
    try:
        response = HostCallResponse.from_binary(
            host_call(
                pause_payload(project),
                platform=replace(
                    Sim(seed=0).platform(
                        threads,
                        processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
                        signaller=FakeProcessSignaller(set()),
                    ),
                    writer_signaller=signaller,
                ),
            )
        )
        assert response.result is not None
        assert response.result.field == "post_intent"
        result = response.result.value.termination
        assert result is not None
        assert result.outcome is p.host_payloads.TerminateRunOutcome.IDENTITY_CHANGED
        assert signaller.opened == []
        assert signaller.terminated == []
    finally:
        successor.close()


def test_internal_terminate_operation_uses_the_same_pause_now_owner(
    project: tuple[Project, str], sim: Sim
) -> None:
    owner, run_id = project
    journal, threads = native_journal(project, sim)
    head = p.RunHead.from_binary(Project.run_head_path_for(owner.root, run_id).read_bytes())
    assert head.process is not None
    signaller = FakeProcessSignaller(
        {head.process.pid},
        held_locks={
            head.process.pid: {
                writer_identity_path(Project.run_log_path_for(owner.root, run_id), head.process)
            }
        },
    )
    request = p.MutateRequest(
        checkout="/src/vibesys",
        method=Oneof(
            "terminate_run",
            p.host_payloads.TerminateRunRequest(
                request_id="pause-1",
                project_root=str(owner.root),
                run_id=run_id,
                expected_process=head.process,
            ),
        ),
    )
    output = BytesIO()
    try:
        assert (
            main(
                ["terminate-run"],
                stdin=BytesIO(request.to_binary()),
                stdout=output,
                platform=replace(
                    sim.platform(
                        threads,
                        processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
                        signaller=signaller,
                    ),
                    writer_signaller=signaller,
                ),
            )
            == 0
        )
        response = p.MutateResponse.from_binary(output.getvalue())
        validate(response)
        assert response.result is not None
        assert response.result.field == "terminate_run"
        assert response.result.value.request_id == "pause-1"
        assert (
            response.result.value.outcome
            is p.host_payloads.TerminateRunOutcome.REQUESTED_UNCONFIRMED
        )
        assert signaller.terminated == [head.process.pid]
        assert owner.state.control_inbox(run_id).pending()[0].request_id == "pause-1"
    finally:
        journal.close()


@pytest.mark.parametrize("operation", ["post-intent", "terminate-run"])
@pytest.mark.parametrize("startup", [0, 1, 4, 8, 11])
@pytest.mark.parametrize("release", [False, True])
def test_gateway_pause_reply_preserves_admission_with_the_remaining_budget(
    project: tuple[Project, str], sim: Sim, startup: int, operation: str, *, release: bool
) -> None:
    owner, run_id = project
    journal, threads = native_journal(project, sim)
    head = p.RunHead.from_binary(Project.run_head_path_for(owner.root, run_id).read_bytes())
    assert head.process is not None
    signaller = FakeProcessSignaller(
        {head.process.pid},
        held_locks={
            head.process.pid: {
                writer_identity_path(Project.run_log_path_for(owner.root, run_id), head.process)
            }
        },
    )
    request = HostCallRequest.from_binary(pause_payload(project))
    cap = next(op.cap_ms / 1000 for op in p.host_operations.HOST_CALLS if op.slot == "post_intent")
    outer_deadline = sim.wall_clock.now() + cap
    request.deadline = p.Timestamp.from_seconds(outer_deadline)
    payload = (
        request.to_binary()
        if operation == "post-intent"
        else p.MutateRequest(
            checkout=request.checkout,
            method=Oneof(
                "terminate_run",
                p.host_payloads.TerminateRunRequest(
                    request_id="pause-1",
                    project_root=str(owner.root),
                    run_id=run_id,
                    expected_process=head.process,
                    deadline=request.deadline,
                ),
            ),
        ).to_binary()
    )
    threads.sleep(startup)
    if release:
        journal.close()
    output = BytesIO()
    try:
        assert (
            main(
                [operation],
                stdin=BytesIO(payload),
                stdout=output,
                platform=replace(
                    sim.platform(
                        threads,
                        processes=FakeProcesses(lambda _: ProcessScript(), pass_time=threads.sleep),
                        signaller=signaller,
                    ),
                    writer_signaller=signaller,
                ),
            )
            == 0
        )
        response = (
            HostCallResponse if operation == "post-intent" else p.MutateResponse
        ).from_binary(output.getvalue())
        validate(response)
        assert response.result is not None
        assert owner.state.control_inbox(run_id).pending()[0].request_id == "pause-1"
        if response.result.field == "post_intent":
            assert response.result.value.request_id == "pause-1"
            result = response.result.value.termination
        else:
            assert response.result.field == "terminate_run"
            result = response.result.value
        assert result is not None
        assert result.outcome is (
            p.host_payloads.TerminateRunOutcome.WRITER_RELEASED
            if release
            else p.host_payloads.TerminateRunOutcome.OBSERVATION_UNKNOWN
            if startup >= 8
            else p.host_payloads.TerminateRunOutcome.REQUESTED_UNCONFIRMED
        )
        assert signaller.terminated == ([head.process.pid] if not release and startup < 8 else [])
        if startup < cap:
            assert sim.wall_clock.now() < outer_deadline
    finally:
        journal.close()
