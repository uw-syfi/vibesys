"""Integration tests for the issue-loop orchestrator.

These tests mock ``vibesys.orchestration.runtime.build_agent_client`` so the real
agent CLI plumbing never executes. Each test exercises one focused
behaviour of the drain-and-perf-eval outer loop in
``vibesys/loops/issue_queue/loop.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

from vibesys.config import as_config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND
from vibesys.errors import ConfigurationError
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.evaluators.perf_reply import (
    IssuePerfEvalResponse,
    PerfMetrics,
)
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.issue_queue.orchestration import IssueQueueOptions, descriptor_from_options
from vibesys.loops.issue_queue.state import IssueQueueStateStore
from vibesys.orchestration.request import ResumeRef, RunRequest
from vibesys.orchestration.runner import run_orchestration
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import IssueImplementerResponse
from vibesys.roles.judge import IssueJudgeResponse
from vibesys.run.integration import LocalRunIntegration
from vibesys.schemas import PerfTrend
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_issue_board.api import IssueBoard, IssueStatus
from vs_project.api import OrchestrationRunManifest, Project, RunEnvironmentRecord


class _SharedFakeClient:
    """Keep one scripted client usable across independently closed role handles."""

    def __init__(self, scripted: FakeAgentClient) -> None:
        self._scripted = scripted

    def __getattr__(self, name: str) -> object:
        return getattr(self._scripted, name)

    def close(self) -> None:
        """Leave the shared script open for the other roles."""


def _share_client(mock_build_runner: MagicMock, fake: FakeAgentClient) -> None:
    mock_build_runner.side_effect = lambda **_kwargs: _SharedFakeClient(fake)


# ---------------------------------------------------------------------------
# Helpers — factories and fixtures shared across tests
# ---------------------------------------------------------------------------


def _run_plain_request(**kwargs) -> bool:  # noqa: ANN003  # tracked: #288
    """Build the canonical descriptor request for an issue-loop fixture."""
    config = as_config(kwargs["config"])
    bundle = load_input_bundle(Path(kwargs["input_path"]))
    profiler = kwargs.get("profiler_kind", ProfilerKind.AUTO)
    backend = kwargs.get("backend", DEFAULT_COMPUTE_BACKEND)
    options = IssueQueueOptions(
        max_rounds=kwargs.get("max_rounds", 5),
        max_attempts_per_issue=kwargs.get("max_attempts_per_issue", 3),
        max_issues_per_perf_eval=kwargs.get("max_issues_per_perf_eval", 3),
    )
    descriptor = descriptor_from_options(options)
    request = RunRequest(
        project_root=bundle.root,
        orchestration=descriptor,
        config=config,
        input_bundle=bundle,
        exp_name=kwargs["exp_name"],
        runs_dir=kwargs.get("runs_dir"),
        resume=ResumeRef(run_id=kwargs["exp_name"]) if kwargs.get("existing") else None,
        debug=kwargs.get("debug", False),
        profiler_kind=profiler,
        run_environment=kwargs.get("run_environment"),
        agent_backend=kwargs.get("agent_backend"),
        cli_provider=kwargs.get("cli_provider"),
        backend=backend,
    )

    async def execute() -> bool:
        integration = LocalRunIntegration()
        try:
            return await run_orchestration(request, integration, IssueQueueOrchestrator(descriptor))
        finally:
            integration.close()

    return asyncio.run(execute())


def _make_impl_resp(issue_id: int, summary: str = "Done.") -> IssueImplementerResponse:
    return IssueImplementerResponse(
        issue_id=issue_id,
        summary=summary,
        files_touched=[],
        self_check="ok",
    )


def _make_judge_resp(
    issue_id: int,
    verdict: str = "pass",
    feedback: str = "",
    new_issues_filed: list[int] | None = None,
) -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=issue_id,
        analysis=f"Analysis for {verdict}.",
        feedback=feedback,
        verdict=Verdict(verdict),
        new_issues_filed=new_issues_filed or [],
    )


def _make_perf_resp(new_issue_ids: list[int] | None = None) -> IssuePerfEvalResponse:
    return IssuePerfEvalResponse(
        analysis="Benchmarked.",
        metrics=PerfMetrics(load_levels=[]),
        evaluator_feedback=[],
        new_issue_ids=new_issue_ids or [],
        throughput_trend=PerfTrend.IMPROVED,
        latency_trend=PerfTrend.IMPROVED,
    )


def _run_exp_dir(tmp_path: Path) -> Path:
    """Return the single canonical project provisioned by the run."""
    projects = sorted(path for path in (tmp_path / "exp_env").iterdir() if path.is_dir())
    assert len(projects) == 1, f"expected one project, got {projects}"
    return projects[0]


def _store_path(exp_dir: Path) -> Path:
    """The agent-visible issue board is project-root workflow memory."""
    return exp_dir / "issues.json"


def _run_id(project_dir: Path) -> str:
    runs = Project.open(project_dir).state.list_runs()
    assert len(runs) == 1, runs
    return runs[0].run_id


def _plain_state_store(project_dir: Path) -> IssueQueueStateStore:
    project = Project.open(project_dir)
    return IssueQueueStateStore(project.state.portable_namespace(_run_id(project_dir), "plain"))


def _plain_local_dir(project_dir: Path) -> Path:
    project = Project.open(project_dir)
    return project.state.local_namespace(_run_id(project_dir), "plain").external_directory()


@pytest.fixture
def ref_file(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    """Create a temporary reference file for _run_plain_request tests."""
    project = tmp_path / "input"
    project.mkdir()
    f = project / "ref.py"
    f.write_text("def predict(x): return x * 2\n")
    (project / "OBJECTIVE.md").write_text("Make the implementation faster.\n")
    (project / "vibesys.input.toml").write_text(
        """version = 1

[agent]
domain = "llm-serving"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('ok')"]
""",
        encoding="utf-8",
    )
    return str(f)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_bootstrap_creates_initial_feature_issue_on_first_run(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    assert result is True
    exp_dir = _run_exp_dir(tmp_path)
    manifest = Project.open(exp_dir).state.load_run(_run_id(exp_dir))
    assert isinstance(manifest, OrchestrationRunManifest)
    assert manifest.run_environment == RunEnvironmentRecord(name="local")
    assert manifest.orchestration.id == "plain"
    assert manifest.orchestration.config_version == 1
    assert manifest.orchestration.options["max_rounds"] == 1
    issues_path = _store_path(exp_dir)
    assert issues_path.is_file()
    data = json.loads(issues_path.read_text())
    assert len(data["issues"]) == 1
    bootstrap = data["issues"][0]
    assert bootstrap["id"] == 1
    assert bootstrap["type"] == "feature"
    assert bootstrap["created_by"] == "loop:bootstrap"
    title = bootstrap["title"]
    assert "FastAPI" in title or "inference server" in title


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_bootstrap_idempotent_on_resume(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """A resumed run with bootstrap_done=True must not re-create the bootstrap issue."""

    # --- First run: create the exp dir and the bootstrap issue. ---
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    first_issues = json.loads(_store_path(exp_dir).read_text())
    assert len(first_issues["issues"]) == 1

    # --- Second run: resume with bootstrap_done=True. ---
    mock_build_runner.reset_mock()
    fake2 = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake2.enqueue(
        "perf_eval", _make_perf_resp(new_issue_ids=[])
    )  # only perf_eval — nothing open to drain
    _share_client(mock_build_runner, fake2)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            runs_dir=tmp_path / "exp_env",
            input_path=str(exp_dir),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            existing=True,
        )

    # Issue count must not increase — no second bootstrap.
    second_issues = json.loads(_store_path(exp_dir).read_text())
    assert len(second_issues["issues"]) == 1
    assert second_issues["issues"][0]["id"] == 1
    assert second_issues["issues"][0]["created_by"] == "loop:bootstrap"


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_v4_plain_budget_increase_requires_clean_workspace(
    mock_build_runner,  # noqa: ANN001
    mock_backend,  # noqa: ANN001, ARG001
    ref_file,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    run_id = _run_id(exp_dir)
    pending = exp_dir / "pending-change.txt"
    pending.write_text("uncommitted candidate change")
    resume = {
        "config": {"model": {"name": "claude-sonnet-4-6"}},
        "exp_name": run_id,
        "runs_dir": tmp_path / "exp_env",
        "input_path": str(exp_dir),
        "accuracy_command": "uv run python accuracy_checker/checker.py",
        "benchmark_command": "uv run python benchmark/benchmark.py",
        "max_rounds": 2,
        "existing": True,
    }
    with (
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        pytest.raises(ConfigurationError, match="commit or discard pending project changes"),
    ):
        _run_plain_request(**resume)
    recorded = Project.open(exp_dir).state.load_run(run_id)
    assert isinstance(recorded, OrchestrationRunManifest)
    assert recorded.orchestration.options["max_rounds"] == 1

    pending.unlink()
    resumed = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    resumed.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    resumed.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, resumed)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(**resume)
    updated = Project.open(exp_dir).state.load_run(run_id)
    assert isinstance(updated, OrchestrationRunManifest)
    assert updated.orchestration.options["max_rounds"] == 2


# ---------------------------------------------------------------------------
# Pass / Fail / Block behaviour during drain
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_judge_pass_closes_issue(mock_build_runner, mock_backend, ref_file, tmp_path):  # noqa: ANN001, ANN201, ARG001  # tracked: #288
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    store = IssueBoard(_store_path(exp_dir))
    issue1 = store.get(1)
    assert issue1 is not None
    assert issue1.status == IssueStatus.CLOSED


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_judge_fail_increments_attempts_and_keeps_open(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """A FAIL verdict reopens the issue; the next drain pass tries again."""
    # impl1 -> judge1(FAIL) -> drain loops back -> impl2 -> judge2(PASS) -> perf
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1), _make_impl_resp(1, summary="Fixed."))
    fake.enqueue(
        "judge",
        _make_judge_resp(1, verdict="fail", feedback="Missing endpoint."),
        _make_judge_resp(1, verdict="pass"),
    )
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_attempts_per_issue=3,
        )

    assert result is True
    exp_dir = _run_exp_dir(tmp_path)
    store = IssueBoard(_store_path(exp_dir))
    issue1 = store.get(1)
    assert issue1 is not None
    assert issue1.attempts == 2
    assert issue1.status == IssueStatus.CLOSED


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_issue_blocks_after_max_attempts_exhausted(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """With max_attempts_per_issue=2, a fail/fail sequence should mark the issue BLOCKED."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1), _make_impl_resp(1))
    fake.enqueue(
        "judge",
        _make_judge_resp(1, verdict="fail", feedback="Still broken."),
        _make_judge_resp(1, verdict="fail", feedback="Still broken."),
    )
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_attempts_per_issue=2,
        )

    assert result is False  # stuck — all remaining blocked
    exp_dir = _run_exp_dir(tmp_path)
    store = IssueBoard(_store_path(exp_dir))
    issue1 = store.get(1)
    assert issue1 is not None
    assert issue1.status == IssueStatus.BLOCKED
    assert issue1.attempts == 2


# ---------------------------------------------------------------------------
# Per-phase MCP server spec passed to AgentClient.invoke
# ---------------------------------------------------------------------------


def _spec_args_to_dict(args: Sequence[str]) -> dict[str, str]:
    """Parse the policy flags out of an MCP server args list.

    The args list is shaped like::

        ["-m", "vs_issue_board.mcp", "issues.json",
         "--creator", "judge", "--iteration", "1",
         "--allowed-types", "bug", "--cap", "1"]

    so we just walk pair-wise looking for the ``--*`` flags.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            out[args[i].lstrip("-")] = args[i + 1]
            i += 2
        else:
            i += 1
    return out


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_judge_invoke_receives_tracker_kwargs(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
):
    """The judge phase must receive issue-tracker access scoped to
    creator='judge', cap=1, allowed_types={BUG}.

    The plain turn helper passes an MCPServerSpec directly to AgentClient.
    """
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_issues_per_perf_eval=3,
        )

    judge_calls = fake.calls_for("judge")
    assert len(judge_calls) == 1
    specs = judge_calls[0].mcp_servers

    assert specs is not None and len(specs) == 1  # noqa: PT018  # tracked: #288
    spec = specs[0]
    assert spec.name == "vibesys-issues"
    assert spec.command == "python"
    parsed = _spec_args_to_dict(spec.args)
    assert parsed["creator"] == "judge"
    assert parsed["cap"] == "1"
    assert parsed["allowed-types"] == "bug"
    assert "issues.json" in spec.args


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_perf_eval_invoke_receives_tracker_kwargs(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
):
    """The perf_eval phase must receive issue-tracker access scoped to
    creator='perf_eval', cap=max_issues_per_perf_eval, and the
    BUG/FEATURE/PERF allowed-types set.

    Passed as ``mcp_servers`` by the plain turn helper.
    """
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_issues_per_perf_eval=2,
        )

    perf_calls = fake.calls_for("perf_eval")
    assert len(perf_calls) == 1
    specs = perf_calls[0].mcp_servers

    assert specs is not None and len(specs) == 1  # noqa: PT018  # tracked: #288
    spec = specs[0]
    parsed = _spec_args_to_dict(spec.args)
    assert parsed["creator"] == "perf_eval"
    assert parsed["cap"] == "2"
    # allowed-types is sorted alphabetically (bug,feature,perf).
    assert parsed["allowed-types"] == "bug,feature,perf"
    assert "issues.json" in spec.args


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_judge_phase_calls_store_reload_after_invoke(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """After the judge invoke returns, the loop must reload the store so it
    can see any issues the MCP server wrote during the phase."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    reload_call_order: list[str] = []
    invoke_call_order: list[str] = []

    original_reload = IssueBoard.reload

    def tracking_reload(self):  # noqa: ANN001, ANN202  # tracked: #288
        reload_call_order.append("reload")
        return original_reload(self)

    fake.on_invoke(lambda call: invoke_call_order.append(call.kind))

    with (
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        patch.object(
            IssueBoard,
            "reload",
            tracking_reload,
        ),
    ):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    # At least one reload happens AFTER the judge invoke (and another after
    # perf_eval). Both phases must call reload.
    assert reload_call_order, "expected at least one store.reload() call"
    assert invoke_call_order == ["implementer", "judge", "perf_eval"]


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_implementer_invoke_has_no_tracker_kwargs(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001  # tracked: #288
):
    """The implementer phase has no issue tools (the issue is inlined in
    the prompt), so it must NOT receive ``mcp_servers``.

    Cleanup of per-provider config files is the runner's responsibility
    and is covered in tests/vibesys/agents/test_agent_runners.py. At the loop level we
    only verify which phases get tracker kwargs.
    """
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    impl_calls = fake.calls_for("implementer")
    assert impl_calls, "expected at least one implementer invoke"
    for c in impl_calls:
        # The injection-point kwarg may be omitted entirely or explicit None.
        assert not c.mcp_servers

    # The judge and perf_eval invokes both DO receive tracker access.
    judge_calls = fake.calls_for("judge")
    perf_calls = fake.calls_for("perf_eval")
    assert len(judge_calls) == 1
    assert len(perf_calls) == 1
    assert judge_calls[0].mcp_servers
    assert perf_calls[0].mcp_servers


# ---------------------------------------------------------------------------
# Call ordering within an iteration
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_perf_eval_runs_after_drain_complete(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """Within one outer iteration, the order of invoke kinds is impl -> judge -> perf_eval."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    kinds = [call.kind for call in fake.calls]
    assert kinds == ["implementer", "judge", "perf_eval"]

    # Check the response_cls keyword for each phase
    response_classes = [call.response_cls for call in fake.calls]
    assert response_classes == [
        IssueImplementerResponse,
        IssueJudgeResponse,
        IssuePerfEvalResponse,
    ]

    # Sanity: each phase still got a rendered (non-empty) system prompt.
    for call in fake.calls:
        sys_prompt = call.system_prompt
        assert isinstance(sys_prompt, str) and sys_prompt.strip()  # noqa: PT018  # tracked: #288


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_resume_with_bootstrap_done_skips_bootstrap_creation(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """Resuming with bootstrap_done=True must not add another bootstrap issue."""

    # Phase 1: fresh run to stand up the exp_dir + git repo.
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=2,
        )

    exp_dir = _run_exp_dir(tmp_path)
    issues_path = _store_path(exp_dir)
    assert len(json.loads(issues_path.read_text())["issues"]) == 1

    # Phase 2: resumed run. No implementer/judge should fire (nothing open),
    # only perf_eval.
    mock_build_runner.reset_mock()
    fake2 = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake2.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake2)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            runs_dir=tmp_path / "exp_env",
            input_path=str(exp_dir),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=2,
            existing=True,
        )

    assert result is True
    # Still exactly one issue — no duplicated bootstrap.
    issues = json.loads(issues_path.read_text())["issues"]
    assert len(issues) == 1
    assert issues[0]["created_by"] == "loop:bootstrap"
    # And only perf_eval was invoked during the resume.
    kinds = [call.kind for call in fake2.calls]
    assert kinds == ["perf_eval"]


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_resume_retries_previously_blocked_issue(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """A run that bailed out with all issues BLOCKED should retry those
    issues on resume. The blocked issue's attempts counter is reset so
    the implementer/judge gets a fresh ``max_attempts_per_issue`` budget.
    """

    # Phase 1: bootstrap issue fails twice -> BLOCKED, loop bails out.
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1), _make_impl_resp(1))
    fake.enqueue(
        "judge",
        _make_judge_resp(1, verdict="fail", feedback="nope"),
        _make_judge_resp(1, verdict="fail", feedback="still nope"),
    )
    _share_client(mock_build_runner, fake)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result1 = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_attempts_per_issue=2,
        )
    assert result1 is False  # stuck — every remaining issue is blocked

    exp_dir = _run_exp_dir(tmp_path)
    pre_resume = IssueBoard(_store_path(exp_dir)).get(1)
    assert pre_resume is not None
    assert pre_resume.status == IssueStatus.BLOCKED
    assert pre_resume.attempts == 2

    # Phase 2: resume. The blocked issue should be reopened with a fresh
    # attempt budget; this time the implementer/judge cycle passes.
    mock_build_runner.reset_mock()
    fake2 = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake2.enqueue("implementer", _make_impl_resp(1, summary="Fixed."))
    fake2.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake2.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake2)
    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result2 = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            runs_dir=tmp_path / "exp_env",
            input_path=str(exp_dir),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_attempts_per_issue=2,
            existing=True,
        )
    assert result2 is True

    post_resume = IssueBoard(_store_path(exp_dir)).get(1)
    assert post_resume is not None
    assert post_resume.status == IssueStatus.CLOSED
    # Reopen reset attempts to 0; the successful retry counts as attempt 1.
    assert post_resume.attempts == 1
    # The blocked->open transition is recorded in history.
    actions = [evt.action for evt in post_resume.history]
    assert "blocked->open" in actions


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_run_returns_true_when_perf_eval_files_no_issues_after_clean_drain(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """Bootstrap -> pass -> perf_eval files nothing => run returns True and stops."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        result = _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    assert result is True
    assert len(fake.calls) == 3


# ---------------------------------------------------------------------------
# State checkpointing
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_state_json_written_with_bootstrap_done_after_run(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """At the end of a successful run, state.json should reflect bootstrap_done=True."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    state_store = _plain_state_store(exp_dir)
    cursor = state_store.load_cursor()
    assert cursor is not None
    assert cursor.bootstrap_done is True
    assert cursor.phase
    assert cursor.round_idx >= 0
    assert state_store.load_performance().records
    assert not (exp_dir / "logs").exists()
    assert not (_plain_local_dir(exp_dir) / "issues.json").exists()


# ---------------------------------------------------------------------------
# Per-issue markdown view (rendered via store on_change callback)
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_issue_loop_writes_per_issue_markdown_via_callback(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """End-to-end: drive a one-iteration drain cycle and assert that
    local ``plain/issues/INDEX.md`` plus a per-issue MD file are written by the
    store's on_change → render_all callback, with the implementer summary
    and judge analysis surfacing in the per-issue markdown."""
    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue("implementer", _make_impl_resp(1, summary="Implemented the streaming endpoint."))
    fake.enqueue("judge", _make_judge_resp(1, verdict="pass"))
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    issues_dir = _plain_local_dir(exp_dir) / "issues"
    assert issues_dir.is_dir(), f"{issues_dir} was not created"

    index_path = issues_dir / "INDEX.md"
    assert index_path.is_file(), "INDEX.md was not written"
    index = index_path.read_text(encoding="utf-8")
    assert "Issue Index" in index

    # Bootstrap issue is id=1; the slug comes from its title.
    issue_files = list(issues_dir.glob("0001-*.md"))
    assert len(issue_files) == 1, f"expected one 0001 file, got {issue_files}"

    issue_md = issue_files[0].read_text(encoding="utf-8")
    # Implementer payload made it through into the rendered MD
    assert "Implemented the streaming endpoint." in issue_md
    # Judge payload section is present (verdict in caps)
    assert "PASS" in issue_md
    # The per-issue file links back to the issue id
    assert "#0001" in issue_md

    assert not (exp_dir / "issues").exists()


# ---------------------------------------------------------------------------
# Implementer retry feedback (judge FAIL → next implementer sees feedback)
# ---------------------------------------------------------------------------


@patch("vibesys.backends.cuda.make_local_shell_sandbox")
@patch("vibesys.orchestration.runtime.build_agent_client")
def test_implementer_retry_user_prompt_includes_prior_judge_feedback(  # noqa: ANN201  # tracked: #288
    mock_build_runner,  # noqa: ANN001  # tracked: #288
    mock_backend,  # noqa: ANN001, ARG001  # tracked: #288
    ref_file,  # noqa: ANN001  # tracked: #288
    tmp_path,  # noqa: ANN001, ARG001, RUF100  # tracked: #288
):
    """When the judge fails an issue and the drain loop retries, the
    second implementer's *user* prompt must include the prior judge
    feedback so the model knows what to fix."""

    fake = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    fake.enqueue(
        "implementer",
        _make_impl_resp(1, summary="First attempt."),
        _make_impl_resp(1, summary="Second attempt."),
    )
    fake.enqueue(
        "judge",
        _make_judge_resp(
            1,
            verdict="fail",
            feedback="Add streaming support to the /v1/completions endpoint.",
        ),
        _make_judge_resp(1, verdict="pass"),
    )
    fake.enqueue("perf_eval", _make_perf_resp(new_issue_ids=[]))
    _share_client(mock_build_runner, fake)

    with patch("vibesys.context.PROJECT_ROOT", tmp_path):
        _run_plain_request(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            runs_dir=tmp_path / "exp_env",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            max_rounds=1,
            max_attempts_per_issue=3,
        )

    # Order: impl1, judge1, impl2, judge2, perf  → second implementer is index 2
    assert len(fake.calls) == 5
    impl_calls = fake.calls_for("implementer")
    assert len(impl_calls) == 2
    second_user_prompt = impl_calls[1].user_prompt

    # The retry user prompt must surface the prior feedback verbatim.
    assert "Add streaming support to the /v1/completions endpoint." in second_user_prompt
    assert "Previous review feedback" in second_user_prompt

    # Sanity: the FIRST implementer's user prompt must NOT contain the
    # feedback section, because there was no prior judge review yet.
    first_user_prompt = impl_calls[0].user_prompt
    assert "Previous review feedback" not in first_user_prompt
