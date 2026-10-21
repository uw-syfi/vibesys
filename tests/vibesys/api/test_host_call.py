"""HostCall protobuf round trips use the real durable inbox, without a child process."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO, StringIO
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.control import control_main
from entrypoints.host import main
from vibesys.api import CONTROL_REQUEST
from vibesys.api.host import host_call
from vs_core.api import RunResultProposal, RunStatus, initial_state
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord, StoredEnvelope
from vs_project.api.testing import run_execution_record
from vs_proto.api import (
    CancelAttemptIntent,
    CancelItemIntent,
    HostCallRequest,
    HostCallResponse,
    Oneof,
    ParkIntent,
    PauseIntent,
    PostIntentRequest,
    ResumeIntent,
    ResumeWhen,
    SetParallelismIntent,
    SteerIntent,
    SteerMode,
    SteerTarget,
    SteerTargetKind,
    StopIntent,
    StopMode,
    WithdrawIntent,
    validate,
)

if TYPE_CHECKING:
    from pathlib import Path

    from protobuf import Message


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


def call(project: Project, run_id: str, intent: Oneof, request_id: str = "r-1") -> HostCallResponse:
    request = HostCallRequest(
        checkout="/src/vibesys",
        method=Oneof(
            "post_intent",
            PostIntentRequest(
                project_root=str(project.root), run_id=run_id, request_id=request_id, intent=intent
            ),
        ),
    )
    response = HostCallResponse.from_binary(host_call(request.to_binary()))
    validate(response)
    return response


@pytest.mark.parametrize(
    ("name", "intent", "action"),
    [
        ("pause", PauseIntent(), "pause"),
        ("resume", ResumeIntent(when=ResumeWhen.NOW), "resume"),
        ("resume", ResumeIntent(when=ResumeWhen.CAPACITY_RETURNS), "resume"),
        ("resume", ResumeIntent(when=ResumeWhen.NOW, fallback=True), "resume"),
        ("stop", StopIntent(mode=StopMode.DRAIN), "stop"),
        ("stop", StopIntent(mode=StopMode.CANCEL), "stop"),
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
    call(owner, run_id, Oneof("resume", ResumeIntent(when=ResumeWhen.NOW)), "a")
    call(owner, run_id, Oneof("pause", PauseIntent()), "z")
    response = call(owner, run_id, Oneof("stop", StopIntent(mode=StopMode.DRAIN)), "z")
    assert response.result is not None
    assert response.result.field == "configuration_failed"
    assert "different request" in response.result.value.message
    assert [entry.request_id for entry in owner.state.control_inbox(run_id).pending()] == ["z", "a"]


@pytest.mark.parametrize(
    "intent",
    [
        Oneof("park", ParkIntent()),
        Oneof("resume", ResumeIntent()),
        Oneof("resume", ResumeIntent(when=ResumeWhen.CAPACITY_RETURNS, fallback=True)),
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
    response = HostCallResponse.from_binary(host_call(payload))
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
    response = HostCallResponse.from_binary(host_call(b"x" * ((1 << 20) + 1)))
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
    store = owner.state_store(run_id)
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    store.commit(
        None,
        StoredEnvelope(
            revision=0,
            schema_version=1,
            payload=json.dumps({"envelope": {"core": ended.model_dump(mode="json")}}).encode(),
        ),
        fence,
        now=0,
    )
    accepted = call(owner, run_id, Oneof("pause", PauseIntent()))
    assert accepted.result is not None
    assert accepted.result.field == "post_intent"
    refused = call(owner, run_id, Oneof("resume", ResumeIntent(when=ResumeWhen.NOW)), "new")
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
        (["resume"], Oneof("resume", ResumeIntent(when=ResumeWhen.NOW))),
        (["stop", "--mode", "cancel"], Oneof("stop", StopIntent(mode=StopMode.CANCEL))),
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
