"""Persisted restart and real-workspace behavior of the explicit multi plugin."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.multi._integration_support import (
    InterruptedTurnError,
    execute,
    load_state,
    options,
    write_input,
)

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.review import Verdict
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeInvocation
from vs_project.api import Project
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel


_CAPABILITIES = AgentCapabilities(
    session_reuse=True,
    provider_session_resume=True,
)


def _delete_loose_git_object(workspace: Path, revision: str) -> None:
    """Remove a newly written object from Git's actual, possibly shared store."""
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-948032 [S603]; fixed Git argv resolves the repository-owned object store without a shell.
        [git, "rev-parse", "--path-format=absolute", "--git-path", "objects"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    loose_object = Path(result.stdout.strip()) / revision[:2] / revision[2:]
    assert loose_object.is_file()
    loose_object.unlink()


def _pre_round() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False,
        profile_focus="",
        reasoning="Existing evidence is sufficient.",
    )


def _plan(hypothesis_id: str = "H-01", **changes: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "hypothesis": "Batching removes per-request overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
            **changes,
        }
    )


def _implementation(summary: str = "Implemented batching.") -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary,
        expected_behavior="Fewer launches.",
        evidence="The local smoke check passed.",
    )


def _judge(verdict: Verdict = Verdict.PASS) -> JudgeResponse:
    return JudgeResponse(
        analysis="The change matches the plan and evidence.",
        feedback="" if verdict is Verdict.PASS else "Repair the boundary check.",
        verdict=verdict,
    )


def _client(
    role: str,
    response: BaseModel | dict[str, object] | None = None,
    *,
    failure: BaseException | None = None,
    on_invoke: Callable[[FakeInvocation], None] | None = None,
) -> FakeAgentClient:
    client = FakeAgentClient(backend_name="stub", capabilities=_CAPABILITIES)
    if failure is not None:
        client.fail(role, failure)
    elif response is not None:
        client.enqueue(role, response)
    if on_invoke is not None:
        client.on_invoke(on_invoke)
    return client


def _successful_clients() -> list[FakeAgentClient]:
    return [
        _client("orchestrator", _pre_round()),
        _client("orchestrator", _plan()),
        _client("implementer", _implementation()),
        _client("judge", _judge()),
    ]


def _interrupted_workspace(input_root: Path) -> tuple[Path, str]:
    projects = list((input_root.parent / f"{input_root.name}-runs").iterdir())
    assert len(projects) == 1
    workspace = projects[0]
    return workspace, Project.open(workspace).state.resolve_run().run_id


def _state_summary(project_root: Path, run_id: str) -> tuple[object, ...]:
    state = load_state(project_root, run_id)
    assert state is not None
    return tuple(
        (
            record.hypothesis_id,
            record.judge_verdict,
            record.passed,
        )
        for record in state.search.rounds
    )


@pytest.mark.parametrize("interrupted_role", ["plan", "implementer", "judge"])
def test_persisted_restart_does_not_repeat_completed_multi_stages(
    tmp_path: Path,
    interrupted_role: str,
) -> None:
    baseline_input = write_input(tmp_path / "baseline-input")
    baseline_status, baseline_id, baseline_workspace = execute(
        baseline_input,
        _successful_clients(),
    )
    baseline_summary = _state_summary(baseline_workspace, baseline_id)

    crashed_input = write_input(tmp_path / "crash-input")
    interruption = InterruptedTurnError(interrupted_role)
    if interrupted_role == "plan":
        crashed_clients = [
            _client("orchestrator", _pre_round()),
            _client("orchestrator", failure=interruption),
        ]
        resumed_clients = _successful_clients()
        expected_roles = [
            "orchestrator",
            "orchestrator",
            "orchestrator",
            "orchestrator",
            "implementer",
            "judge",
        ]
    elif interrupted_role == "implementer":
        crashed_clients = [
            _client("orchestrator", _pre_round()),
            _client("orchestrator", _plan()),
            _client("implementer", failure=interruption),
        ]
        resumed_clients = [
            _client("implementer", _implementation()),
            _client("judge", _judge()),
        ]
        expected_roles = ["orchestrator", "orchestrator", "implementer", "implementer", "judge"]
    else:
        crashed_clients = [
            _client("orchestrator", _pre_round()),
            _client("orchestrator", _plan()),
            _client("implementer", _implementation()),
            _client("judge", failure=interruption),
        ]
        resumed_clients = [
            _client("implementer", _implementation("Rechecked batching.")),
            _client("judge", _judge()),
        ]
        expected_roles = [
            "orchestrator",
            "orchestrator",
            "implementer",
            "judge",
            "implementer",
            "judge",
        ]

    with pytest.raises(InterruptedTurnError, match=interrupted_role):
        execute(crashed_input, crashed_clients)

    resumed_workspace, interrupted_id = _interrupted_workspace(crashed_input)
    resumed_status, resumed_id, _ = execute(
        resumed_workspace,
        resumed_clients,
        resume_run_id=interrupted_id,
    )

    assert baseline_status is resumed_status is RunStatus.SUCCEEDED
    assert resumed_id == interrupted_id
    assert _state_summary(resumed_workspace, resumed_id) == baseline_summary
    resumed_state = load_state(resumed_workspace, resumed_id)
    assert resumed_state is not None
    assert resumed_state.search.rounds[0].attempts == (1 if interrupted_role == "plan" else 2)
    assert [
        call.kind for client in (*crashed_clients, *resumed_clients) for call in client.calls
    ] == expected_roles


def test_judge_write_is_reverted_before_the_next_attempt(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "input")
    stray_path = "progress/evidence/round-0001-attempt-02-judge.json"

    def stray_write(invocation: FakeInvocation) -> None:
        target = invocation.workspace / stray_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"verdict": "pass"}\n', encoding="utf-8")

    clients = [
        _client("orchestrator", _pre_round()),
        _client("orchestrator", _plan()),
        _client("implementer", _implementation()),
        _client("judge", _judge(Verdict.FAIL)),
        _client("judge", _judge(), on_invoke=stray_write),
    ]
    clients[2].enqueue("implementer", _implementation("Repaired the boundary check."))

    status, _run_id, workspace = execute(input_root, clients)

    assert status is RunStatus.SUCCEEDED
    assert not (workspace / stray_path).exists()
    assert [call.kind for client in clients for call in client.calls].count("judge") == 2


@pytest.mark.usefixtures("loose_git_objects")
def test_failed_rollback_restore_warns_and_continues(tmp_path: Path) -> None:
    input_root = write_input(tmp_path / "input")

    def delete_round_one_revision(invocation: FakeInvocation) -> None:
        run = Project.open(invocation.workspace).state.resolve_run()
        state = load_state(invocation.workspace, run.run_id)
        assert state is not None
        assert len(state.search.rounds) == 1
        revision = state.search.rounds[0].commit
        assert revision is not None
        _delete_loose_git_object(invocation.workspace, revision)

    clients = [
        _client("orchestrator", _pre_round()),
        _client("orchestrator", _plan()),
        _client("implementer", _implementation("Round one batching.")),
        _client("judge", _judge()),
        _client("orchestrator", _pre_round()),
        _client(
            "orchestrator",
            _plan("H-02", revert_to_round=1),
            on_invoke=delete_round_one_revision,
        ),
        _client("implementer", _implementation("Round two batching.")),
        _client("judge", _judge()),
    ]

    status, run_id, workspace = execute(
        input_root,
        clients,
        configured=options(max_rounds=2),
    )

    state = load_state(workspace, run_id)
    assert status is RunStatus.SUCCEEDED
    assert state is not None
    hypothesis = state.search.by_id("H-02")
    assert hypothesis is not None
    assert hypothesis.revert_applied is False
    assert hypothesis.revert_commit is None
    assert len(state.search.rounds) == 2
