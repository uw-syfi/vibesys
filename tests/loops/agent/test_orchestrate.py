"""Tests for vibe_serve.loops.agent — orchestrator-driven build loop."""

from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.agents import AgentRunner
from vibe_serve.domains.base import DomainName
from vibe_serve.loops.agent import issue_board
from vibe_serve.loops.agent.loop import run_agent_loop
from vibe_serve.profilers import ProfilerKind
from vibe_serve.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    Verdict,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def ref_file(tmp_path):
    """Create a reference *file* (not dir) + an OBJECTIVE.md sibling.

    Using a single file avoids the model-weight lookup that a reference
    directory triggers, which keeps these tests independent of HF cache
    state.
    """
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    return str(ref)


def _make_orchestrate_runner(
    *,
    pre_decisions: list[PreRoundDecision] | None = None,
    plans: list[OrchestratorPlan] | None = None,
    judge_verdicts: list[str] | None = None,
    profiler_responses: list[ProfilerSummary] | None = None,
):
    """Build a MagicMock AgentRunner whose invoke() returns scripted responses.

    Arguments are consumed-in-order queues keyed by the agent kind / response
    class. Defaults: when the plan queue is exhausted, the harness returns a
    permissive no-op plan and lets the loop's ``max_rounds`` bound the test.
    Judge verdicts default to pass; the profiler is not called.
    """
    pre_q = list(pre_decisions or [])
    plan_q = list(plans or [])
    judge_q = list(judge_verdicts or [])
    prof_q = list(profiler_responses or [])
    counters = {"impl": 0, "judge": 0, "orch_pre": 0, "orch_plan": 0, "prof": 0}

    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = "deepagents"

    def _invoke(*, kind, response_cls, fallback_factory, **kwargs):
        if kind == "orchestrator" and response_cls is PreRoundDecision:
            counters["orch_pre"] += 1
            if pre_q:
                return pre_q.pop(0)
            return PreRoundDecision(need_profile=False, profile_focus="", reasoning="default skip")
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            counters["orch_plan"] += 1
            if plan_q:
                return plan_q.pop(0)
            return OrchestratorPlan(
                task="noop (harness default)",
                pass_criteria="no criteria",
                reasoning="default noop plan — the loop's max_rounds bounds the test",
            )
        if kind == "implementer":
            counters["impl"] += 1
            return ImplementerResponse(summary="Done.", expected_behavior="ok")
        if kind == "judge":
            idx = counters["judge"]
            counters["judge"] += 1
            v = judge_q[idx] if idx < len(judge_q) else "pass"
            return JudgeResponse(
                analysis="ok",
                feedback="" if v == "pass" else "needs work",
                verdict=Verdict.PASS if v == "pass" else Verdict.FAIL,
            )
        if kind == "profiler":
            counters["prof"] += 1
            if prof_q:
                return prof_q.pop(0)
            return ProfilerSummary(
                analysis="ok",
                bottlenecks="none",
                suggestions="none",
            )
        raise AssertionError(f"unexpected kind: {kind}, response_cls={response_cls}")

    runner.invoke.side_effect = _invoke
    runner.counters = counters  # test introspection
    return runner


def _invoke_orchestrate(tmp_path, ref_file, runner, **kwargs):
    """Shared plumbing: patch context globals, run the loop, return result."""
    defaults = dict(
        config={"model": {"name": "claude-sonnet-4-6"}},
        exp_name="test-orch",
        reference_path=ref_file,
        objective="Maximize tok/s throughput.",
        max_rounds=5,
        max_retries_per_round=2,
    )
    defaults.update(kwargs)
    with (
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.backends.cuda.LocalShellBackend"),
        patch("vibe_serve.context.build_agent_runner", return_value=runner),
        patch("vibe_serve.context.PROJECT_ROOT", tmp_path),
    ):
        return run_agent_loop(**defaults)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def test_pre_round_decision_accepts_booleans():
    d = PreRoundDecision(need_profile=True, profile_focus="decode kernels", reasoning="ok")
    assert d.need_profile is True
    assert d.profile_focus == "decode kernels"


def test_orchestrator_plan_revert_round_optional():
    p = OrchestratorPlan(
        task="redo",
        pass_criteria="passes tests",
        revert_to_round=3,
        reasoning="step back",
    )
    assert p.revert_to_round == 3


def test_profiler_summary_perf_metric_optional():
    p = ProfilerSummary(analysis="a", bottlenecks="b", suggestions="s")
    assert p.perf_metric is None
    p2 = ProfilerSummary(
        analysis="a",
        bottlenecks="b",
        suggestions="s",
        perf_metric=12.5,
        perf_unit="tok/s",
    )
    assert p2.perf_metric == 12.5
    assert p2.perf_unit == "tok/s"


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------


def test_progress_writes_orchestrator_plan(tmp_path):
    progress = tmp_path / "progress.md"
    plan = OrchestratorPlan(
        task="Build FastAPI server",
        pass_criteria="/health returns 200",
        reasoning="Round 1 cold start",
    )
    issue_board.append_orchestrator_plan(progress, 1, plan)
    text = progress.read_text()
    assert "Round 1 — Orchestrator (plan)" in text
    assert "Build FastAPI server" in text
    assert "/health returns 200" in text


def test_progress_writes_profiler_summary_with_perf(tmp_path):
    progress = tmp_path / "progress.md"
    summary = ProfilerSummary(
        analysis="launch-bound",
        bottlenecks="attention kernel 40%",
        suggestions="swap to flashinfer",
        perf_metric=8.2,
        perf_unit="req/s",
    )
    issue_board.append_profiler_summary(progress, 2, summary)
    text = progress.read_text()
    assert "Round 2 — Profiler" in text
    assert "perf_metric**: 8.2 req/s" in text
    assert "flashinfer" in text


def test_progress_append_implementer_and_judge(tmp_path):
    progress = tmp_path / "progress.md"
    issue_board.append_implementer(
        progress,
        3,
        1,
        ImplementerResponse(summary="added cuda graph", expected_behavior="replay works"),
    )
    issue_board.append_judge(
        progress,
        3,
        1,
        JudgeResponse(analysis="good", feedback="", verdict=Verdict.PASS),
    )
    text = progress.read_text()
    assert "Round 3 — Implementer (attempt 1)" in text
    assert "Round 3 — Judge (attempt 1)" in text
    assert "verdict**: pass" in text


# ---------------------------------------------------------------------------
# Loop happy paths
# ---------------------------------------------------------------------------


def test_loop_round_one_no_profile_runs_one_round(tmp_path, ref_file):
    """Round 1 skips pre-round-decision (no existing code), proposes one task,
    implementer+judge both pass. With max_rounds=1 the loop stops there."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build FastAPI server",
                pass_criteria="/health returns 200",
                reasoning="cold start",
            ),
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    # No pre-round decision on round 1 (no existing code).
    assert runner.counters["orch_plan"] == 1
    assert runner.counters["impl"] == 1
    assert runner.counters["judge"] == 1
    assert runner.counters["prof"] == 0


def test_loop_judge_retry_then_pass(tmp_path, ref_file):
    """Judge fails once, implementer retries, judge passes. Loop bounded by max_rounds=1."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="tests pass",
                reasoning="cold start",
            ),
        ],
        judge_verdicts=["fail", "pass"],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1, max_retries_per_round=3)
    assert result is True
    assert runner.counters["impl"] == 2
    assert runner.counters["judge"] == 2


def test_loop_exhaustion_carries_to_next_round(tmp_path, ref_file):
    """Judge loop exhausts on round 1, orchestrator in round 2 sees exhaustion,
    proposes easier task, that one passes, then done on round 3."""
    seen_plan_prompts: list[str] = []
    original_runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build the whole server with every optimization",
                pass_criteria="impossibly strict",
                reasoning="ambitious",
            ),
            OrchestratorPlan(
                task="Just get /health working",
                pass_criteria="/health returns 200",
                reasoning="backed off after exhaustion",
            ),
        ],
        judge_verdicts=["fail", "fail", "pass"],
    )

    # Wrap invoke so we can capture the orchestrator plan prompts.
    real_invoke = original_runner.invoke.side_effect

    def spy_invoke(*, kind, response_cls, **kwargs):
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_plan_prompts.append(kwargs.get("system_prompt", ""))
        return real_invoke(kind=kind, response_cls=response_cls, **kwargs)

    original_runner.invoke.side_effect = spy_invoke

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        original_runner,
        max_rounds=2,
        max_retries_per_round=2,
    )
    assert result is True
    # 2 attempts on round 1 (both fail) + 1 attempt on round 2 (pass).
    assert original_runner.counters["impl"] == 3
    # Round 2's plan prompt must contain the exhaustion signal.
    assert len(seen_plan_prompts) >= 2
    assert "exhausted" in seen_plan_prompts[1].lower()


def test_loop_orchestrator_requests_profile_before_plan(tmp_path, ref_file):
    """If PreRoundDecision.need_profile is True, profiler runs before the plan call."""
    call_order: list[str] = []
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="need data"),
        ],
        plans=[
            # Round 1 cold-start plan (no pre-decision invoked on round 1).
            OrchestratorPlan(
                task="Build server",
                pass_criteria="ok",
                reasoning="start",
            ),
            # Round 2 plan — uses profiler summary.
            OrchestratorPlan(
                task="Optimize decode",
                pass_criteria="graph replay",
                reasoning="profile showed launch overhead",
            ),
        ],
        profiler_responses=[
            ProfilerSummary(
                analysis="launch-bound",
                bottlenecks="host-side sync",
                suggestions="cuda graph",
                perf_metric=5.0,
                perf_unit="req/s",
            ),
        ],
    )

    real_invoke = runner.invoke.side_effect

    def spy_invoke(*, kind, response_cls, **kwargs):
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            call_order.append("plan")
        elif kind == "profiler":
            call_order.append("profiler")
        elif kind == "orchestrator" and response_cls is PreRoundDecision:
            call_order.append("pre")
        return real_invoke(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy_invoke

    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=2)
    assert result is True
    # Round 1 cold-start: no pre → just plan.
    # Round 2: pre → profiler → plan.
    assert call_order[:1] == ["plan"]
    assert "profiler" in call_order
    plan_idx = [i for i, c in enumerate(call_order) if c == "plan"]
    prof_idx = call_order.index("profiler")
    # Profiler must come BEFORE the round-2 plan call.
    assert prof_idx < plan_idx[1]


def test_loop_skips_profiler_when_pre_round_decision_says_no(tmp_path, ref_file):
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=False, profile_focus="", reasoning="benchmark is enough"),
        ],
        plans=[
            OrchestratorPlan(task="Build server", pass_criteria="ok", reasoning="start"),
            OrchestratorPlan(task="Use benchmark evidence", pass_criteria="ok", reasoning="skip"),
        ],
    )

    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=2)

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 0


def test_loop_skips_profiler_when_profiler_kind_is_none(tmp_path, ref_file):
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="would help"),
        ],
        plans=[
            OrchestratorPlan(task="Build server", pass_criteria="ok", reasoning="start"),
            OrchestratorPlan(
                task="Use benchmark evidence", pass_criteria="ok", reasoning="disabled"
            ),
        ],
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        profiler_kind=ProfilerKind.NONE,
    )

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 0


def test_loop_generic_auto_profiler_resolves_to_none(tmp_path, ref_file):
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="kernels", reasoning="would help"),
        ],
        plans=[
            OrchestratorPlan(task="Build queue", pass_criteria="ok", reasoning="start"),
            OrchestratorPlan(
                task="Use benchmark evidence", pass_criteria="ok", reasoning="generic"
            ),
        ],
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        max_rounds=2,
        domain=DomainName.GENERIC,
    )

    assert result is True
    assert runner.counters["orch_pre"] == 1
    assert runner.counters["prof"] == 0


def test_loop_runs_full_max_rounds_budget(tmp_path, ref_file):
    """With the ``done`` field removed, the loop always exhausts max_rounds.
    A single-round budget yields one implementer + judge call, no more."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="ok",
                reasoning="round 1",
            )
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    assert runner.counters["impl"] == 1
    assert runner.counters["judge"] == 1


def test_loop_max_rounds_terminates(tmp_path, ref_file):
    """Loop exits after max_rounds and reports success (the loop always runs
    to budget; there is no early-stop signal)."""
    plans = [OrchestratorPlan(task=f"t{i}", pass_criteria="p", reasoning="r") for i in range(10)]
    runner = _make_orchestrate_runner(plans=plans)
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=3)
    assert result is True
    assert runner.counters["orch_plan"] == 3
    assert runner.counters["impl"] == 3


# ---------------------------------------------------------------------------
# CLI / OBJECTIVE.md discovery
# ---------------------------------------------------------------------------


def test_cli_loads_objective_md_from_ref_parent(tmp_path):
    from vibe_serve.cli import _load_objective

    ref_dir = tmp_path / "modelA" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")
    (ref_dir.parent / "OBJECTIVE.md").write_text(
        "Maximize throughput (tok/s). Prefer CUDA graphs.\n"
    )
    objective = _load_objective(str(ref_dir))
    assert "Maximize throughput" in objective


def test_cli_missing_objective_md_errors(tmp_path):
    from vibe_serve.cli import _load_objective

    ref_dir = tmp_path / "modelB" / "reference"
    ref_dir.mkdir(parents=True)
    (ref_dir / "reference.py").write_text("pass\n")
    with pytest.raises(FileNotFoundError, match="OBJECTIVE.md"):
        _load_objective(str(ref_dir))


def test_cli_rejects_modal_with_nsys_profiler(tmp_path, ref_file):
    """--modal only supports torch profiler."""
    from vibe_serve.cli import _build_agent_parser, _validate_agent

    parser = _build_agent_parser()
    validate_args = _validate_agent
    args = parser.parse_args(
        [
            "--ref",
            ref_file,
            "--exp-name",
            "test",
            "--modal",
            "--profiler",
            "nsys",
        ]
    )
    with pytest.raises(SystemExit):
        validate_args(args)


# ---------------------------------------------------------------------------
# --resume semantics
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Roadmap + plateau detection
# ---------------------------------------------------------------------------


def test_ensure_roadmap_seeds_header_when_missing(tmp_path):
    from vibe_serve.loops.agent import issue_board

    p = tmp_path / "roadmap.md"
    assert not p.exists()
    issue_board.ensure_roadmap_file(p)
    assert p.exists()
    text = p.read_text()
    # The seed must scaffold the four sections so the orchestrator's first
    # round starts with a clear structure.
    assert "## Major" in text
    assert "## Minor" in text
    assert "## Done" in text
    assert "## Abandoned" in text


def test_ensure_roadmap_does_not_overwrite_existing(tmp_path):
    from vibe_serve.loops.agent import issue_board

    p = tmp_path / "roadmap.md"
    p.write_text("# my custom plan\n")
    issue_board.ensure_roadmap_file(p)
    assert p.read_text() == "# my custom plan\n"


def test_read_roadmap_returns_text(tmp_path):
    from vibe_serve.loops.agent import issue_board

    p = tmp_path / "roadmap.md"
    p.write_text("hello\n")
    assert issue_board.read_roadmap(p) == "hello\n"


def test_read_roadmap_missing_returns_empty(tmp_path):
    from vibe_serve.loops.agent import issue_board

    p = tmp_path / "nope.md"
    assert issue_board.read_roadmap(p) == ""


def _record(round_number: int, perf: float | None, unit: str = "tok/s"):
    """Build a _RoundRecord shorthand for plateau tests."""
    from vibe_serve.loops.agent.loop import _RoundRecord

    return _RoundRecord(
        round_number=round_number,
        commit=f"sha{round_number:03d}",
        perf_metric=perf,
        perf_unit=unit if perf is not None else None,
        passed=perf is not None,
    )


def test_detect_plateau_returns_none_when_too_few_rounds():
    from vibe_serve.loops.agent.loop import _detect_plateau

    # Two rounds is below the 3-round minimum streak.
    records = [_record(1, 40.0), _record(2, 41.0)]
    assert _detect_plateau(records) is None


def test_detect_plateau_fires_on_flat_perf_streak():
    from vibe_serve.loops.agent.loop import _detect_plateau

    # 41.0 vs 41.5 is ~1.2% spread — well under the 5% threshold.
    records = [_record(1, 41.0), _record(2, 41.5), _record(3, 41.2)]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–3" in warning
    assert "tok/s" in warning


def test_detect_plateau_skips_when_perf_diverges():
    from vibe_serve.loops.agent.loop import _detect_plateau

    # 41.0 vs 116.0 is ~64% spread — clearly off-plateau.
    records = [_record(1, 41.0), _record(2, 116.0), _record(3, 114.5)]
    assert _detect_plateau(records) is None


def test_detect_plateau_ignores_rounds_without_perf():
    """Rounds where the profiler skipped or the round failed (perf=None) must
    not interrupt the streak — only valid measurements count."""
    from vibe_serve.loops.agent.loop import _detect_plateau

    records = [
        _record(1, 41.0),
        _record(2, None),  # profiler skipped or failed round
        _record(3, 41.3),
        _record(4, 41.1),
    ]
    warning = _detect_plateau(records)
    assert warning is not None
    assert "rounds 1–4" in warning


def test_detect_plateau_streak_must_be_recent():
    """A plateau early in the run that's followed by a clear win must NOT
    fire a warning on the next round — only the *last N* matter."""
    from vibe_serve.loops.agent.loop import _detect_plateau

    records = [
        _record(1, 41.0),  # plateau
        _record(2, 41.2),  # plateau
        _record(3, 41.1),  # plateau (would fire here)
        _record(4, 116.0),  # break
    ]
    # By round 4, the recent streak (rounds 2,3,4) spans 41.2-116.0 → no plateau.
    assert _detect_plateau(records) is None


def test_loop_creates_roadmap_md_in_workspace(tmp_path, ref_file):
    """The first round of a fresh run must seed roadmap.md in the workspace."""
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(
                task="Build server",
                pass_criteria="/health 200",
                reasoning="cold start",
            ),
        ],
    )
    result = _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert result is True
    # The workspace lives under exp_env/<run-dir>/workspace/.
    roadmap_files = list((tmp_path / "exp_env").glob("*/workspace/roadmap.md"))
    assert len(roadmap_files) == 1
    text = roadmap_files[0].read_text()
    assert "## Major" in text


def test_loop_threads_roadmap_into_orchestrator_prompt(tmp_path, ref_file):
    """The orchestrator's plan prompt must include the current roadmap.md
    contents so the orchestrator can update them."""
    seen_prompts: list[str] = []
    runner = _make_orchestrate_runner(
        plans=[
            OrchestratorPlan(task="t", pass_criteria="p", reasoning="r"),
        ],
    )
    real = runner.invoke.side_effect

    def spy(*, kind, response_cls, **kwargs):
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_prompts.append(kwargs.get("system_prompt", ""))
        return real(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy

    _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=1)
    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    # Roadmap section header must be present, and so must the seed scaffold.
    assert "Roadmap" in prompt
    assert "Major" in prompt
    assert "roadmap.md" in prompt


def test_loop_threads_plateau_warning_into_prompt(tmp_path, ref_file):
    """When the prior rounds plateau on perf, the orchestrator's next prompt
    must include the plateau warning."""
    seen_prompts: list[str] = []
    # Five rounds: round 1 is cold-start (no profiler), rounds 2-4 produce
    # flat perf metrics, and round 5 is the round under test (its plan call
    # should see the plateau warning).
    plans = [
        OrchestratorPlan(task=f"r{i}", pass_criteria="p", reasoning=f"r{i}") for i in range(1, 6)
    ]
    runner = _make_orchestrate_runner(
        pre_decisions=[
            PreRoundDecision(need_profile=True, profile_focus="x", reasoning="ok"),
        ]
        * 4,  # rounds 2-5
        plans=plans,
        profiler_responses=[
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.0,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.1,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=41.9,
                perf_unit="tok/s",
            ),
            ProfilerSummary(
                analysis="a",
                bottlenecks="b",
                suggestions="s",
                perf_metric=42.05,
                perf_unit="tok/s",
            ),
        ],
    )
    real = runner.invoke.side_effect

    def spy(*, kind, response_cls, **kwargs):
        if kind == "orchestrator" and response_cls is OrchestratorPlan:
            seen_prompts.append(kwargs.get("system_prompt", ""))
        return real(kind=kind, response_cls=response_cls, **kwargs)

    runner.invoke.side_effect = spy

    _invoke_orchestrate(tmp_path, ref_file, runner, max_rounds=5)
    assert len(seen_prompts) == 5
    # Rounds 1-4 have <3 valid perf records before each plan call → no
    # warning yet (round 1: 0 perf; round 2: 0 perf; round 3: 1 perf; round 4: 2 perf).
    for i in range(4):
        assert "Plateau detected" not in seen_prompts[i], (
            f"round {i + 1} should not yet have plateau warning"
        )
    # Round 5 plan call sees rounds 1-4 in records (3 valid perf measurements
    # from rounds 2,3,4 — flat at 41.9-42.1) → warning fires.
    assert "Plateau detected" in seen_prompts[4]


def test_loop_resume_with_round_number_starts_there(tmp_path, ref_file):
    """--resume 4 starts the loop at round 4 (prior rounds were committed by previous run)."""
    # With start_round=4 and max_rounds=5 only rounds 4 and 5 execute.
    plans = [
        OrchestratorPlan(task="keep going", pass_criteria="tests pass", reasoning="round 4"),
        OrchestratorPlan(task="more work", pass_criteria="tests pass", reasoning="round 5"),
    ]
    runner = _make_orchestrate_runner(plans=plans)

    # Pre-seed an existing exp dir so the context init takes the `existing=True`
    # branch.
    exp_env = tmp_path / "exp_env"
    (exp_env / "20260422-000000-test-orch").mkdir(parents=True)
    # Minimal git setup so the context validation accepts the repo.
    import subprocess

    subprocess.run(
        ["git", "init"],
        cwd=exp_env / "20260422-000000-test-orch",
        capture_output=True,
        check=True,
    )
    ws = exp_env / "20260422-000000-test-orch" / "workspace"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=ws, capture_output=True, check=True)
    (ws / "dummy.txt").write_text("x")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=ws, env={**env}, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=ws, env={**env}, capture_output=True, check=True
    )

    result = _invoke_orchestrate(
        tmp_path,
        ref_file,
        runner,
        exp_name="20260422-000000-test-orch",
        existing=True,
        start_round=4,
        max_rounds=5,
    )
    assert result is True
    # Round 4 and 5 only: 2 plan calls (one task, one done).
    assert runner.counters["orch_plan"] == 2
