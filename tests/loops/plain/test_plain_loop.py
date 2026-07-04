"""Integration tests for the issue-loop orchestrator.

These tests mock ``vibe_serve.context.build_agent_runner`` so the real
LangChain / CLI plumbing never executes. Each test exercises one focused
behaviour of the drain-and-perf-eval outer loop in
``vibe_serve/plain/loop.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.agents import AgentRunner
from vibe_serve.loops.plain.loop import PlainLoopState, run_plain_loop
from vibe_serve.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_issue_board import IssueBoard, IssueStatus

# ---------------------------------------------------------------------------
# Helpers — factories and fixtures shared across tests
# ---------------------------------------------------------------------------


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


def _make_issue_runner(responses: list, *, backend_name: str = "deepagents") -> MagicMock:
    """Mock AgentRunner.invoke that yields scripted responses in order.

    The script is consumed left-to-right regardless of ``kind``, so the test
    just lists responses in the order the loop will call invoke().

    The mock is wrapped in PlainLoopAgentRunner inside ``run_plain_loop``,
    so the calls recorded on this mock reflect what the wrapper passed
    after injecting tracker tools / MCP server specs based on
    ``backend_name``.
    """
    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = backend_name
    it = iter(responses)

    def _invoke(*, kind, **kwargs):
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError(f"invoke called beyond scripted responses (kind={kind})") from exc

    runner.invoke.side_effect = _invoke
    return runner


def _run_exp_dir(tmp_path: Path) -> Path:
    """Return the single exp_env/<timestamp>-test directory produced by a run."""
    exp_dirs = sorted((tmp_path / "exp_env").glob("*-test"))
    assert len(exp_dirs) == 1, f"expected one exp dir, got {exp_dirs}"
    return exp_dirs[0]


def _store_path(exp_dir: Path) -> Path:
    """The canonical issues.json lives inside the unified workspace."""
    return exp_dir / "workspace" / "issues.json"


@pytest.fixture()
def ref_file(tmp_path):
    """Create a temporary reference file for run_plain_loop tests."""
    f = tmp_path / "ref.py"
    f.write_text("def predict(x): return x * 2\n")
    return str(f)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_bootstrap_creates_initial_feature_issue_on_first_run(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    assert result is True
    exp_dir = _run_exp_dir(tmp_path)
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


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_bootstrap_idempotent_on_resume(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """A resumed run with bootstrap_done=True must not re-create the bootstrap issue."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"

    # --- First run: create the exp dir and the bootstrap issue. ---
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    first_issues = json.loads(_store_path(exp_dir).read_text())
    assert len(first_issues["issues"]) == 1

    # --- Second run: resume with bootstrap_done=True. ---
    mock_build_runner.reset_mock()
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_perf_resp(new_issue_ids=[]),  # only perf_eval — nothing open to drain
        ]
    )
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            reference_path=ref_file,
            max_rounds=1,
            existing=True,
            resume_state=PlainLoopState(bootstrap_done=True),
        )

    # Issue count must not increase — no second bootstrap.
    second_issues = json.loads(_store_path(exp_dir).read_text())
    assert len(second_issues["issues"]) == 1
    assert second_issues["issues"][0]["id"] == 1
    assert second_issues["issues"][0]["created_by"] == "loop:bootstrap"


# ---------------------------------------------------------------------------
# Pass / Fail / Block behaviour during drain
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_judge_pass_closes_issue(mock_build_runner, mock_backend, mock_build, ref_file, tmp_path):
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    store = IssueBoard(_store_path(exp_dir))
    issue1 = store.get(1)
    assert issue1 is not None
    assert issue1.status == IssueStatus.CLOSED


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_judge_fail_increments_attempts_and_keeps_open(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """A FAIL verdict reopens the issue; the next drain pass tries again."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    # impl1 -> judge1(FAIL) -> drain loops back -> impl2 -> judge2(PASS) -> perf
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="fail", feedback="Missing endpoint."),
            _make_impl_resp(1, summary="Fixed."),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
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


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_issue_blocks_after_max_attempts_exhausted(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """With max_attempts_per_issue=2, a fail/fail sequence should mark the issue BLOCKED."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="fail", feedback="Still broken."),
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="fail", feedback="Still broken."),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
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
# Per-phase MCP server spec passed to AgentRunner.invoke
# ---------------------------------------------------------------------------


def _spec_args_to_dict(args: list[str]) -> dict[str, str]:
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


_EXPECTED_TRACKER_TOOL_NAMES = {
    "list_issues",
    "get_issue",
    "search_issues",
    "create_issue",
}


@pytest.mark.parametrize("backend_name", ["deepagents", "cli"])
@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_judge_invoke_receives_tracker_kwargs(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path, backend_name
):
    """The judge phase must receive issue-tracker access scoped to
    creator='judge', cap=1, allowed_types={BUG}.

    The PlainLoopAgentRunner wrapper picks the right transport based on
    the inner runner's backend_name: ``tools`` (in-process @tool callables)
    for the deepagents backend, ``mcp_servers`` (an MCPServerSpec) for
    the cli backend.
    """
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    runner = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ],
        backend_name=backend_name,
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
            max_issues_per_perf_eval=3,
        )

    judge_calls = [c for c in runner.invoke.call_args_list if c.kwargs.get("kind") == "judge"]
    assert len(judge_calls) == 1
    kwargs = judge_calls[0].kwargs

    if backend_name == "cli":
        assert kwargs.get("tools") is None
        specs = kwargs.get("mcp_servers")
        assert specs is not None and len(specs) == 1
        spec = specs[0]
        assert spec.name == "vibeserve-issues"
        assert spec.command == "python"
        parsed = _spec_args_to_dict(spec.args)
        assert parsed["creator"] == "judge"
        assert parsed["cap"] == "1"
        assert parsed["allowed-types"] == "bug"
        assert "issues.json" in spec.args
    else:  # deepagents
        assert kwargs.get("mcp_servers") is None
        tools = kwargs.get("tools")
        assert tools is not None
        assert {t.name for t in tools} == _EXPECTED_TRACKER_TOOL_NAMES


@pytest.mark.parametrize("backend_name", ["deepagents", "cli"])
@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_perf_eval_invoke_receives_tracker_kwargs(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path, backend_name
):
    """The perf_eval phase must receive issue-tracker access scoped to
    creator='perf_eval', cap=max_issues_per_perf_eval, and the
    BUG/FEATURE/PERF allowed-types set.

    Picks ``tools`` vs ``mcp_servers`` based on the inner runner's
    backend_name (see PlainLoopAgentRunner).
    """
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    runner = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ],
        backend_name=backend_name,
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
            max_issues_per_perf_eval=2,
        )

    perf_calls = [c for c in runner.invoke.call_args_list if c.kwargs.get("kind") == "perf_eval"]
    assert len(perf_calls) == 1
    kwargs = perf_calls[0].kwargs

    if backend_name == "cli":
        assert kwargs.get("tools") is None
        specs = kwargs.get("mcp_servers")
        assert specs is not None and len(specs) == 1
        spec = specs[0]
        parsed = _spec_args_to_dict(spec.args)
        assert parsed["creator"] == "perf_eval"
        assert parsed["cap"] == "2"
        # allowed-types is sorted alphabetically (bug,feature,perf).
        assert parsed["allowed-types"] == "bug,feature,perf"
        assert "issues.json" in spec.args
    else:  # deepagents
        assert kwargs.get("mcp_servers") is None
        tools = kwargs.get("tools")
        assert tools is not None
        assert {t.name for t in tools} == _EXPECTED_TRACKER_TOOL_NAMES


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_judge_phase_calls_store_reload_after_invoke(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """After the judge invoke returns, the loop must reload the store so it
    can see any issues the MCP server wrote during the phase."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    reload_call_order: list[str] = []
    invoke_call_order: list[str] = []

    original_reload = IssueBoard.reload

    def tracking_reload(self):
        reload_call_order.append("reload")
        return original_reload(self)

    def tracking_invoke(*, kind, **kwargs):
        invoke_call_order.append(kind)
        if kind == "implementer":
            return _make_impl_resp(1)
        if kind == "judge":
            return _make_judge_resp(1, verdict="pass")
        if kind == "perf_eval":
            return _make_perf_resp(new_issue_ids=[])
        raise AssertionError(f"unexpected kind: {kind}")

    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = "deepagents"
    runner.invoke.side_effect = tracking_invoke
    mock_build_runner.return_value = runner

    with (
        patch("vibe_serve.context.PROJECT_ROOT", tmp_path),
        patch.object(
            IssueBoard,
            "reload",
            tracking_reload,
        ),
    ):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    # At least one reload happens AFTER the judge invoke (and another after
    # perf_eval). Both phases must call reload.
    assert reload_call_order, "expected at least one store.reload() call"
    assert invoke_call_order == ["implementer", "judge", "perf_eval"]


@pytest.mark.parametrize("backend_name", ["deepagents", "cli"])
@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_implementer_invoke_has_no_tracker_kwargs(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path, backend_name
):
    """The implementer phase has no issue tools (the issue is inlined in
    the prompt), so it must NOT receive ``mcp_servers`` or ``tools``.

    Cleanup of per-provider config files is the runner's responsibility
    and is covered in tests/test_agent_runners.py — at the loop level we
    only verify which phases get tracker kwargs.
    """
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    runner = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ],
        backend_name=backend_name,
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    impl_calls = [c for c in runner.invoke.call_args_list if c.kwargs.get("kind") == "implementer"]
    assert impl_calls, "expected at least one implementer invoke"
    for c in impl_calls:
        # Both injection-point kwargs may be omitted entirely or explicit None.
        assert not c.kwargs.get("mcp_servers")
        assert not c.kwargs.get("tools")

    # The judge and perf_eval invokes both DO receive a tracker kwarg.
    # Which one depends on the backend (see PlainLoopAgentRunner).
    judge_calls = [c for c in runner.invoke.call_args_list if c.kwargs.get("kind") == "judge"]
    perf_calls = [c for c in runner.invoke.call_args_list if c.kwargs.get("kind") == "perf_eval"]
    assert len(judge_calls) == 1
    assert len(perf_calls) == 1
    if backend_name == "cli":
        assert judge_calls[0].kwargs["mcp_servers"]
        assert perf_calls[0].kwargs["mcp_servers"]
    else:
        assert judge_calls[0].kwargs["tools"]
        assert perf_calls[0].kwargs["tools"]


# ---------------------------------------------------------------------------
# Call ordering within an iteration
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_perf_eval_runs_after_drain_complete(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """Within one outer iteration, the order of invoke kinds is impl -> judge -> perf_eval."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    runner = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    kinds = [call.kwargs["kind"] for call in runner.invoke.call_args_list]
    assert kinds == ["implementer", "judge", "perf_eval"]

    # Check the response_cls keyword for each phase
    response_classes = [call.kwargs["response_cls"] for call in runner.invoke.call_args_list]
    assert response_classes == [
        IssueImplementerResponse,
        IssueJudgeResponse,
        IssuePerfEvalResponse,
    ]

    # Sanity: each phase still got a rendered (non-empty) system prompt.
    for call in runner.invoke.call_args_list:
        sys_prompt = call.kwargs.get("system_prompt")
        assert isinstance(sys_prompt, str) and sys_prompt.strip()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_resume_with_bootstrap_done_skips_bootstrap_creation(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """Resuming with bootstrap_done=True must not add another bootstrap issue."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"

    # Phase 1: fresh run to stand up the exp_dir + git repo.
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    issues_path = _store_path(exp_dir)
    assert len(json.loads(issues_path.read_text())["issues"]) == 1

    # Phase 2: resumed run. No implementer/judge should fire (nothing open),
    # only perf_eval.
    mock_build_runner.reset_mock()
    runner2 = _make_issue_runner([_make_perf_resp(new_issue_ids=[])])
    mock_build_runner.return_value = runner2
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            reference_path=ref_file,
            max_rounds=1,
            existing=True,
            resume_state=PlainLoopState(bootstrap_done=True),
        )

    assert result is True
    # Still exactly one issue — no duplicated bootstrap.
    issues = json.loads(issues_path.read_text())["issues"]
    assert len(issues) == 1
    assert issues[0]["created_by"] == "loop:bootstrap"
    # And only perf_eval was invoked during the resume.
    kinds = [call.kwargs["kind"] for call in runner2.invoke.call_args_list]
    assert kinds == ["perf_eval"]


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_resume_retries_previously_blocked_issue(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """A run that bailed out with all issues BLOCKED should retry those
    issues on resume. The blocked issue's attempts counter is reset so
    the implementer/judge gets a fresh ``max_attempts_per_issue`` budget.
    """
    mock_build.return_value = "anthropic:claude-sonnet-4-6"

    # Phase 1: bootstrap issue fails twice -> BLOCKED, loop bails out.
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="fail", feedback="nope"),
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="fail", feedback="still nope"),
        ]
    )
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result1 = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
            max_attempts_per_issue=2,
        )
    assert result1 is False  # stuck — every remaining issue is blocked

    exp_dir = _run_exp_dir(tmp_path)
    pre_resume = IssueBoard(_store_path(exp_dir)).get(1)
    assert pre_resume.status == IssueStatus.BLOCKED
    assert pre_resume.attempts == 2

    # Phase 2: resume. The blocked issue should be reopened with a fresh
    # attempt budget; this time the implementer/judge cycle passes.
    mock_build_runner.reset_mock()
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1, summary="Fixed."),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result2 = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name=exp_dir.name,
            reference_path=ref_file,
            max_rounds=1,
            max_attempts_per_issue=2,
            existing=True,
            resume_state=PlainLoopState(bootstrap_done=True),
        )
    assert result2 is True

    post_resume = IssueBoard(_store_path(exp_dir)).get(1)
    assert post_resume.status == IssueStatus.CLOSED
    # Reopen reset attempts to 0; the successful retry counts as attempt 1.
    assert post_resume.attempts == 1
    # The blocked->open transition is recorded in history.
    actions = [evt.action for evt in post_resume.history]
    assert "blocked->open" in actions


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_run_returns_true_when_perf_eval_files_no_issues_after_clean_drain(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """Bootstrap -> pass -> perf_eval files nothing => run returns True and stops."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    runner = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        result = run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    assert result is True
    assert runner.invoke.call_count == 3


# ---------------------------------------------------------------------------
# State checkpointing
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_state_json_written_with_bootstrap_done_after_run(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """At the end of a successful run, state.json should reflect bootstrap_done=True."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    state_path = exp_dir / "logs" / "state.json"
    assert state_path.is_file()
    data = json.loads(state_path.read_text())
    assert data["bootstrap_done"] is True
    assert "phase" in data
    assert "round_idx" in data
    assert data["version"] == 1


# ---------------------------------------------------------------------------
# Per-issue markdown view (rendered via store on_change callback)
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_issue_loop_writes_per_issue_markdown_via_callback(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """End-to-end: drive a one-iteration drain cycle and assert that
    ``logs/issues/INDEX.md`` plus a per-issue MD file are written by the
    store's on_change → render_all callback, with the implementer summary
    and judge analysis surfacing in the per-issue markdown."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"
    mock_build_runner.return_value = _make_issue_runner(
        [
            _make_impl_resp(1, summary="Implemented the streaming endpoint."),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
        )

    exp_dir = _run_exp_dir(tmp_path)
    issues_dir = exp_dir / "logs" / "issues"
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

    # Workspace mirror copy exists too
    workspace_issues = exp_dir / "workspace" / "issues"
    assert workspace_issues.is_dir()
    assert (workspace_issues / "INDEX.md").is_file()
    assert (workspace_issues / issue_files[0].name).is_file()


# ---------------------------------------------------------------------------
# Implementer retry feedback (judge FAIL → next implementer sees feedback)
# ---------------------------------------------------------------------------


@patch("vibe_serve.context._build_model")
@patch("vibe_serve.backends.cuda.LocalShellBackend")
@patch("vibe_serve.context.build_agent_runner")
def test_implementer_retry_user_prompt_includes_prior_judge_feedback(
    mock_build_runner, mock_backend, mock_build, ref_file, tmp_path
):
    """When the judge fails an issue and the drain loop retries, the
    second implementer's *user* prompt must include the prior judge
    feedback so the model knows what to fix."""
    mock_build.return_value = "anthropic:claude-sonnet-4-6"

    runner = _make_issue_runner(
        [
            _make_impl_resp(1, summary="First attempt."),
            _make_judge_resp(
                1,
                verdict="fail",
                feedback="Add streaming support to the /v1/completions endpoint.",
            ),
            _make_impl_resp(1, summary="Second attempt."),
            _make_judge_resp(1, verdict="pass"),
            _make_perf_resp(new_issue_ids=[]),
        ]
    )
    mock_build_runner.return_value = runner

    with patch("vibe_serve.context.PROJECT_ROOT", tmp_path):
        run_plain_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test",
            reference_path=ref_file,
            max_rounds=1,
            max_attempts_per_issue=3,
        )

    # Order: impl1, judge1, impl2, judge2, perf  → second implementer is index 2
    calls = runner.invoke.call_args_list
    assert len(calls) == 5
    second_impl_call = calls[2]
    assert second_impl_call.kwargs["kind"] == "implementer"
    second_user_prompt = second_impl_call.kwargs["user_prompt"]

    # The retry user prompt must surface the prior feedback verbatim.
    assert "Add streaming support to the /v1/completions endpoint." in second_user_prompt
    assert "Previous review feedback" in second_user_prompt

    # Sanity: the FIRST implementer's user prompt must NOT contain the
    # feedback section, because there was no prior judge review yet.
    first_impl_call = calls[0]
    assert first_impl_call.kwargs["kind"] == "implementer"
    first_user_prompt = first_impl_call.kwargs["user_prompt"]
    assert "Previous review feedback" not in first_user_prompt
