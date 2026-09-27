"""Legacy-compatible golden trajectories through the explicit single plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_events_snapshot,
    assert_prompt_snapshot,
    prompt_text,
    read_events,
)
from tests.vibesys.orchestrations.single._integration_support import (
    execute,
    options,
    profile_options,
    write_input,
)

from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api import AgentCapabilities, AgentTurnTimeoutError
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project
from vs_runtime.api import RunStatus

if TYPE_CHECKING:
    from pathlib import Path


_STRATEGY = "single_plugin"


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # fixture text, not a credential.
        reasoning="scripted golden fixture",
    )


def _combined(
    summary: str = "batched the prefill step",
    *,
    verdict: Verdict = Verdict.PASS,
    feedback: str = "",
) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary=summary,
        expected_behavior="higher steady-state throughput",
        self_review="reviewed the diff and the checks",
        feedback=feedback,
        verdict=verdict,
        bottlenecks="prefill launch overhead dominates at low batch sizes",
        suggestions="batch decode requests next",
        profile_analysis="ran the local checks",
    )


def _client(*, backend_name: str = "stub") -> FakeAgentClient:
    return FakeAgentClient(
        backend_name=backend_name,
        capabilities=AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
        ),
    )


def _golden_input(root: Path) -> Path:
    input_root = write_input(
        root,
        domain="llm-serving",
        objective="Maximize tok/s throughput.",
    )
    (input_root / "queue.py").unlink()
    (input_root / "ref.py").write_text("def predict(x):\n    return x * 2\n", encoding="utf-8")
    (input_root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "llm-serving"\n'
        '[accuracy]\ncommand = ["python", "-c", "print(\'ok\')"]\n'
        '[benchmark]\ncommand = ["python", "-c", "print(\'ok\')"]\n',
        encoding="utf-8",
    )
    return input_root


def _assert_golden(
    *,
    strategy: str,
    scenario: str,
    calls: tuple[FakeAgentClient, ...],
    run_id: str,
    workspace: Path,
) -> None:
    implementer_turn = 0
    for client in calls:
        for call in client.calls:
            if call.kind == "orchestrator":
                label = "orchestrator-round-1-plan"
            else:
                implementer_turn += 1
                label = f"implementer-round-1-retry-{implementer_turn}-single-agent"
            assert_prompt_snapshot(
                strategy,
                label,
                scenario,
                prompt_text(call.system_prompt, call.user_prompt),
                workspace=workspace,
            )

    for relative in ("progress.md", "roadmap.md", "pareto-frontier.md"):
        path = workspace / relative
        if path.is_file():
            assert_board_snapshot(
                strategy,
                scenario,
                relative,
                path.read_text(encoding="utf-8"),
                workspace=workspace,
            )
    plans_dir = workspace / "progress-artifacts" / "plans"
    if plans_dir.is_dir():
        for path in sorted(plans_dir.glob("*.json")):
            relative = path.relative_to(workspace).as_posix()
            assert_board_snapshot(
                strategy,
                scenario,
                relative,
                path.read_text(encoding="utf-8"),
                workspace=workspace,
            )

    log_dir = Project.open(workspace).state.log_directory(run_id)
    event_path = log_dir / "core-events.jsonl"
    assert_events_snapshot(
        strategy,
        scenario,
        read_events(event_path, workspace=workspace),
    )


def test_plain_pass_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.enqueue("implementer", _combined())
    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=options(max_rounds=1, interface="inprocess"),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy=_STRATEGY,
        scenario="pass",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_plain_retry_then_pass_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.enqueue(
        "implementer",
        _combined(
            "first attempt: partial batching",
            verdict=Verdict.FAIL,
            feedback="batching only covers the prefill path, not decode",
        ),
        _combined("second attempt: full batching after self-review feedback"),
    )
    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=options(max_rounds=1, interface="inprocess"),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy=_STRATEGY,
        scenario="retry_then_pass",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_plain_timeout_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.fail("implementer", AgentTurnTimeoutError(30.0), times=1)

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=options(max_rounds=1, interface="inprocess").model_copy(
            update={"max_retries_per_round": 1}
        ),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy=_STRATEGY,
        scenario="timeout",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_plain_gate_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client(backend_name="claude")
    designer.enqueue("orchestrator", _plan())
    implementer = _client(backend_name="claude")
    implementer.enqueue("implementer", _combined())

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=options(max_rounds=1, interface="inprocess"),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy=_STRATEGY,
        scenario="gate",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_profile_single_pass_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.enqueue("implementer", _combined())

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=profile_options(),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy="profile_single_plugin",
        scenario="pass",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_profile_single_retry_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.enqueue(
        "implementer",
        _combined(
            "first attempt: partial batching",
            verdict=Verdict.FAIL,
            feedback="batching only covers the prefill path, not decode",
        ),
        _combined("second attempt: full batching after self-review feedback"),
    )

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=profile_options(),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy="profile_single_plugin",
        scenario="retry_then_pass",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_profile_single_gate_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client(backend_name="claude")
    designer.enqueue("orchestrator", _plan())
    implementer = _client(backend_name="claude")
    implementer.enqueue("implementer", _combined())

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=profile_options(),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy="profile_single_plugin",
        scenario="gate",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )


def test_profile_single_timeout_golden_through_explicit_plugin(tmp_path: Path) -> None:
    input_root = _golden_input(tmp_path / "input")
    designer = _client()
    designer.enqueue("orchestrator", _plan())
    implementer = _client()
    implementer.fail("implementer", AgentTurnTimeoutError(30.0), times=1)
    implementer.enqueue("implementer", _combined())

    status, run_id, workspace = execute(
        input_root,
        [designer, implementer],
        options_override=profile_options(),
    )

    assert status is RunStatus.SUCCEEDED
    _assert_golden(
        strategy="profile_single_plugin",
        scenario="timeout",
        calls=(designer, implementer),
        run_id=run_id,
        workspace=workspace,
    )
