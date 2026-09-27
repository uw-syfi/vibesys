"""Persisted restart and golden behavior for the explicit single plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestrations.single._integration_support import (
    InterruptedTurnError,
    execute,
    load_state,
    write_input,
)

from vibesys.events import CoreEventType, FrameworkSource, FrameworkWarningData
from vibesys.orchestrations.single import PLUGIN
from vibesys.orchestrations.single.models import SingleState
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from vs_agent.api.testing import FakeInvocation


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="Batching removes per-request overhead.",
        title="Batch prefill",
        task="Batch prefill requests.",
        pass_criteria="Throughput improves without an accuracy regression.",  # noqa: S106  # LW-040200 [S106]; this is a plan-contract fixture, not a credential.
        reasoning="The trace shows repeated launch overhead.",
    )


def _rollback_plan() -> OrchestratorPlan:
    return _plan().model_copy(update={"hypothesis_id": "H-02", "revert_to_round": 1})


def _response() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="Implemented batching.",
        expected_behavior="Fewer launches.",
        self_review="Correctness checks passed.",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="Launch overhead.",
        suggestions="Try larger batches.",
        profile_analysis="Local checks passed.",
    )


def _implementer(*, failure: BaseException | None = None) -> FakeAgentClient:
    client = _client()
    if failure is None:
        client.enqueue("implementer", _response())

        def write_candidate(invocation: FakeInvocation) -> None:
            (invocation.workspace / "queue.py").write_text("VALUE = 2\n", encoding="utf-8")

        client.on_invoke(write_candidate)
    else:
        client.fail("implementer", failure)
    return client


def _designer(*, failure: BaseException | None = None) -> FakeAgentClient:
    client = _client()
    if failure is None:
        client.enqueue("orchestrator", _plan())
    else:
        client.fail("orchestrator", failure)
    return client


def _client() -> FakeAgentClient:
    return FakeAgentClient(
        backend_name="stub",
        capabilities=AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
        ),
    )


def _state_summary(project_root: Path, run_id: str) -> tuple[object, ...]:
    state = load_state(project_root, run_id)
    assert state is not None
    return tuple(
        (
            item.hypothesis_id,
            item.passed,
            item.hypothesis_claim,
        )
        for item in state.search.rounds
    )


@pytest.mark.parametrize("interrupted_role", ["orchestrator", "implementer"])
def test_persisted_restart_matches_uninterrupted_single_trajectory(
    tmp_path: Path, interrupted_role: str
) -> None:
    baseline_input = write_input(tmp_path / "baseline-input")
    baseline_status, baseline_id, baseline_workspace = execute(
        baseline_input,
        [_designer(), _implementer()],
    )
    baseline_summary = _state_summary(baseline_workspace, baseline_id)

    crashed_input = write_input(tmp_path / "crash-input")
    crash = InterruptedTurnError(interrupted_role)
    if interrupted_role == "orchestrator":
        clients = [_designer(failure=crash)]
    else:
        clients = [_designer(), _implementer(failure=crash)]

    with pytest.raises(InterruptedTurnError):
        execute(crashed_input, clients)

    projects = list((crashed_input.parent / f"{crashed_input.name}-runs").iterdir())
    assert len(projects) == 1
    resumed_workspace = projects[0]
    interrupted_run = Project.open(resumed_workspace).state.resolve_run()
    resumed_clients = (
        [_designer(), _implementer()] if interrupted_role == "orchestrator" else [_implementer()]
    )
    resumed_status, resumed_id, _ = execute(
        resumed_workspace,
        resumed_clients,
        resume_run_id=interrupted_run.run_id,
    )

    assert baseline_status is resumed_status is RunStatus.SUCCEEDED
    assert resumed_id == interrupted_run.run_id
    assert _state_summary(resumed_workspace, resumed_id) == baseline_summary
    assert (baseline_workspace / "queue.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (resumed_workspace / "queue.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    expected_calls = (
        ["orchestrator", "orchestrator", "implementer"]
        if interrupted_role == "orchestrator"
        else ["orchestrator", "implementer", "implementer"]
    )
    actual_calls = [call.kind for client in (*clients, *resumed_clients) for call in client.calls]
    assert actual_calls == expected_calls


def test_explicit_plugin_pass_trajectory_has_stable_durable_outputs(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "input")
    status, run_id, workspace = execute(
        input_root,
        [_designer(), _implementer()],
    )

    state = load_state(workspace, run_id)
    assert state is not None
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "pass_trajectory.json").read_text(encoding="utf-8")
    )
    actual = {
        "status": status.value,
        "rounds": [
            {
                "hypothesis_id": record.hypothesis_id,
                "claim": record.hypothesis_claim,
                "passed": record.passed,
            }
            for record in state.search.rounds
        ],
        "last_response_summary": state.last_response.summary if state.last_response else None,
        "workspace_files": {
            "queue.py": (workspace / "queue.py").read_text(encoding="utf-8"),
        },
        "artifacts": [
            relative for relative in expected["artifacts"] if (workspace / relative).is_file()
        ],
    }
    assert actual == expected


def test_corrupt_rollback_target_warns_and_commits_the_next_round(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "rollback-input")
    first_designer = _client()
    first_designer.enqueue("orchestrator", _plan())
    second_designer = _client()
    second_designer.enqueue(
        "orchestrator",
        _plan().model_copy(update={"hypothesis_id": "H-02", "revert_to_round": 1}),
    )

    def invalidate_target(invocation: FakeInvocation) -> None:
        if invocation.kind != "orchestrator":
            return
        project = Project.open(invocation.workspace)
        run_id = project.state.resolve_run().run_id
        state = (
            project.state.portable_namespace(run_id, PLUGIN.id)
            .slot("state.json", SingleState)
            .load_optional()
        )
        assert state is not None
        commit = state.search.rounds[0].commit
        assert commit is not None
        loose_object = invocation.workspace / ".git" / "objects" / commit[:2] / commit[2:]
        assert loose_object.is_file()
        loose_object.unlink()

    second_designer.on_invoke(invalidate_target)
    events = []
    status, run_id, workspace = execute(
        input_root,
        [first_designer, _implementer(), second_designer, _implementer()],
        max_rounds=2,
        observed_events=events,
    )

    state = load_state(workspace, run_id)
    assert status is RunStatus.SUCCEEDED
    assert state is not None
    assert len(state.search.rounds) == 2
    second = state.search.by_id("H-02")
    assert second is not None
    assert second.revert_applied is False
    assert second.revert_commit is None
    assert (workspace / "queue.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    warnings = [
        event.data
        for event in events
        if event.type is CoreEventType.FRAMEWORK_WARNING
        and isinstance(event.data, FrameworkWarningData)
        and event.data.source_label == "rollback"
    ]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.source is FrameworkSource.GIT_TRACKING
    assert warning.source_label == "rollback"
    assert warning.detail is None
    assert warning.summary.startswith("could not restore workspace to revision")


def test_successful_rollback_restores_the_selected_revision(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "rollback-success-input")
    first_designer = _client()
    first_designer.enqueue("orchestrator", _plan())
    second_designer = _client()
    second_designer.enqueue("orchestrator", _rollback_plan())

    def diverge_before_rollback(invocation: FakeInvocation) -> None:
        assert invocation.kind == "orchestrator"
        (invocation.workspace / "queue.py").write_text("VALUE = 3\n", encoding="utf-8")

    second_designer.on_invoke(diverge_before_rollback)
    second_implementer = _client()
    second_implementer.enqueue("implementer", _response())

    def verify_restored_tree(invocation: FakeInvocation) -> None:
        assert invocation.kind == "implementer"
        candidate = invocation.workspace / "queue.py"
        assert candidate.read_text(encoding="utf-8") == "VALUE = 2\n"
        candidate.write_text("VALUE = 2\n", encoding="utf-8")

    second_implementer.on_invoke(verify_restored_tree)

    status, run_id, workspace = execute(
        input_root,
        [first_designer, _implementer(), second_designer, second_implementer],
        max_rounds=2,
    )

    state = load_state(workspace, run_id)
    assert status is RunStatus.SUCCEEDED
    assert state is not None
    hypothesis = state.search.by_id("H-02")
    assert hypothesis is not None
    assert hypothesis.revert_applied is True
    assert hypothesis.revert_commit is not None
