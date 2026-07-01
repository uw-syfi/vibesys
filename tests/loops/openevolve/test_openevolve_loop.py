from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vibe_serve.agents import AgentRunner
from vibe_serve.loops.openevolve.loop import run_openevolve_loop
from vibe_serve.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict


@pytest.fixture()
def ref_file(tmp_path):
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    return str(ref)


def _make_runner(captured_progress: list[str | None]):
    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = "deepagents"

    def _invoke(*, kind, response_cls, fallback_factory, progress=None, **kwargs):
        captured_progress.append(progress.label() if progress is not None else None)
        if response_cls is MutatorResponse:
            return MutatorResponse(
                summary="mutated",
                hypothesis="faster",
                expected_behavior="ok",
            )
        if kind == "judge":
            return JudgeResponse(
                analysis="ok",
                feedback="",
                verdict=Verdict.PASS,
            )
        if kind == "profiler":
            return ProfilerSummary(
                analysis="ok",
                bottlenecks="none",
                suggestions="none",
                perf_metric=10.0,
                perf_unit="tok/s",
            )
        raise AssertionError(f"unexpected invoke call: kind={kind} response_cls={response_cls}")

    runner.invoke.side_effect = _invoke
    return runner


def test_openevolve_uses_candidate_progress_for_agent_calls(tmp_path, ref_file):
    captured_progress: list[str | None] = []
    runner = _make_runner(captured_progress)

    with (
        patch("vibe_serve.context._build_model", return_value="mock-model"),
        patch("vibe_serve.backends.cuda.LocalShellBackend"),
        patch("vibe_serve.context.build_agent_runner", return_value=runner),
        patch("vibe_serve.context.PROJECT_ROOT", tmp_path),
    ):
        result = run_openevolve_loop(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test-openevolve",
            reference_path=ref_file,
            objective="Maximize tok/s throughput.",
            max_iterations=1,
            seed=0,
        )

    assert result is True
    assert captured_progress == [
        "Round 1/1 Cand 1/1",
        "Round 1/1 Cand 1/1",
        "Round 1/1 Cand 1/1",
    ]
