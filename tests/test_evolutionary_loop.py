"""Integration tests for the evolutionary search loop.

Mocks the agent runner so the LLM-driven mutator/judge/profiler return
scripted responses. The real ``_RunContext`` is built on a tmp_path
workspace (so git tracking, snapshots, and population persistence are
exercised end-to-end), but the model + sandbox + agent-runner factories
are patched out — same pattern as ``tests/test_orchestrate.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibeserve_agent.agents import AgentRunner
from vibeserve_agent.loops.evolve.loop import run_evolve_loop
from vibeserve_agent.loops.evolve.population import (
    Individual,
    Objective,
    Population,
)
from vibeserve_agent.schemas import MutatorResponse
from vibeserve_agent.schemas import ProfilerSummary
from vibeserve_agent.schemas import JudgeResponse, Verdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ref_file(tmp_path):
    """Reference *file* + sibling OBJECTIVE.md.

    A single-file reference avoids the model-weight resolution that a
    reference *directory* triggers, the same trick test_orchestrate.py
    uses to keep tests independent of HF cache state.
    """
    model_dir = tmp_path / "input_model"
    model_dir.mkdir()
    ref = model_dir / "ref.py"
    ref.write_text("def predict(x): return x * 2\n")
    (model_dir / "OBJECTIVE.md").write_text("Maximize tok/s throughput.\n")
    return str(ref)


def _make_runner(
    *,
    judge_verdicts: list[str] | None = None,
    profiler_responses: list[ProfilerSummary] | None = None,
    capture_mutator_prompts: list[str] | None = None,
):
    """Build a MagicMock AgentRunner with scripted responses.

    The mutator (``kind="implementer"`` + ``response_cls=MutatorResponse``)
    always returns a stub MutatorResponse. Judge verdicts default to
    ``"pass"``; profiler responses default to a fitness of ``10.0 tok/s``
    incrementing by 1 per call so each child has a distinct perf number.
    """
    judge_q = list(judge_verdicts or [])
    prof_q = list(profiler_responses or [])
    counters = {"mutator": 0, "judge": 0, "profiler": 0}

    runner = MagicMock(spec=AgentRunner)
    runner.backend_name = "deepagents"

    def _invoke(*, kind, response_cls, fallback_factory, system_prompt="", **kwargs):
        if response_cls is MutatorResponse:
            counters["mutator"] += 1
            if capture_mutator_prompts is not None:
                capture_mutator_prompts.append(system_prompt)
            return MutatorResponse(
                summary=f"mutator call {counters['mutator']}",
                hypothesis="should be faster",
                expected_behavior="ok",
            )
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
            idx = counters["profiler"]
            counters["profiler"] += 1
            if idx < len(prof_q):
                return prof_q[idx]
            return ProfilerSummary(
                analysis="ok",
                bottlenecks="none",
                suggestions="none",
                perf_metric=10.0 + idx,
                perf_unit="tok/s",
            )
        raise AssertionError(
            f"unexpected agent_runner.invoke call: kind={kind} response_cls={response_cls}"
        )

    runner.invoke.side_effect = _invoke
    runner.counters = counters
    return runner


def _invoke_loop(tmp_path, ref_file, runner, **kwargs):
    """Shared plumbing — patch context globals, run the loop, return result."""
    defaults = dict(
        config={"model": {"name": "claude-sonnet-4-6"}},
        exp_name="test-evolve",
        reference_path=ref_file,
        objective="Maximize tok/s throughput.",
        max_generations=2,
        children_per_generation=1,
        seed=0,
    )
    defaults.update(kwargs)
    with (
        patch("vibeserve_agent.context._build_model", return_value="mock-model"),
        patch("vibeserve_agent.backends.cuda.LocalShellBackend"),
        patch("vibeserve_agent.context.build_agent_runner", return_value=runner),
        patch("vibeserve_agent.context.PROJECT_ROOT", tmp_path),
    ):
        return run_evolve_loop(**defaults)


def _load_population(tmp_path) -> Population:
    """Find the population.json the loop wrote and load it back.

    The exp_dir is named ``<timestamp>-<exp_name>`` so we glob for the
    one entry that exists in the test's tmp_path.
    """
    exp_envs = list((tmp_path / "exp_env").iterdir())
    assert len(exp_envs) == 1, exp_envs
    pop_path = exp_envs[0] / "logs" / "population.json"
    return Population.load(pop_path)


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


def test_cold_start_records_first_individual_with_no_parent(tmp_path, ref_file):
    """Generation 1 child 1 has no parent (population empty); a passing
    judge produces an Individual with parent_id=None and a perf_metric
    from the profiler."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=1, children_per_generation=1,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    seed = pop.all[0]
    assert seed.id == 1
    assert seed.parent_id is None
    assert seed.passed is True
    assert seed.perf_metric == 10.0
    assert seed.perf_unit == "tok/s"
    assert seed.commit  # an actual git SHA was recorded


def test_cold_start_skips_profiler_when_judge_fails(tmp_path, ref_file):
    """A failed cold-start child doesn't get profiled — its Individual
    is recorded with passed=False and no commit."""
    runner = _make_runner(judge_verdicts=["fail"])
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=1, children_per_generation=1,
    )
    assert result is True
    assert runner.counters["mutator"] == 1
    assert runner.counters["judge"] == 1
    assert runner.counters["profiler"] == 0  # judged-fail children skip profiling

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop.all[0]
    assert failed.passed is False
    assert failed.commit is None
    assert failed.perf_metric is None
    assert "needs work" in failed.feedback


# ---------------------------------------------------------------------------
# Multi-generation: parent selection + lineage tracking
# ---------------------------------------------------------------------------


def test_second_generation_uses_first_passing_child_as_parent(tmp_path, ref_file):
    """After gen 1 produces a passing seed, gen 2's child must be tagged
    with parent_id pointing at that seed."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=2, children_per_generation=1,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    seed, child = pop.all
    assert seed.parent_id is None
    assert child.parent_id == seed.id
    assert child.passed is True
    # Both individuals were profiled separately; their perf numbers should
    # be the two distinct values our stub generated (10.0 and 11.0).
    assert {seed.perf_metric, child.perf_metric} == {10.0, 11.0}


def test_failed_child_excluded_from_future_parent_pool(tmp_path, ref_file):
    """Gen 1: pass. Gen 2: fail (no commit, not eligible as parent).
    Gen 3: must still parent off Gen 1 — never off the failed Gen 2."""
    # 3 mutators, 3 judges (pass, fail, pass), 2 profilers (pass children only).
    runner = _make_runner(judge_verdicts=["pass", "fail", "pass"])
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=3, children_per_generation=1,
    )
    assert result is True
    assert runner.counters["mutator"] == 3
    assert runner.counters["judge"] == 3
    assert runner.counters["profiler"] == 2  # only the passes

    pop = _load_population(tmp_path)
    assert len(pop) == 3
    g1, g2, g3 = pop.all
    assert g1.passed is True
    assert g2.passed is False
    assert g2.commit is None
    # The third child must descend from the seed (id=1), NOT from the
    # failed g2 (id=2). g2 had no commit, so it can't be selected.
    assert g3.parent_id == g1.id


# ---------------------------------------------------------------------------
# Mutator prompt content
# ---------------------------------------------------------------------------


def test_cold_start_prompt_uses_cold_start_section(tmp_path, ref_file):
    """The first child sees the cold-start branch of the mutator prompt."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=1, children_per_generation=1,
    )
    assert len(captured) == 1
    prompt = captured[0]
    assert "Cold start" in prompt
    # No parent block when the population is empty.
    assert "Parent (the implementation you are mutating)" not in prompt


# ---------------------------------------------------------------------------
# Pareto / multi-objective mode
# ---------------------------------------------------------------------------


def test_pareto_mode_records_metrics_dict_on_individuals(tmp_path, ref_file):
    """When objectives are configured the loop should pass `objectives` through
    to selection AND copy `ProfilerSummary.metrics` onto every passing
    Individual so the frontier can be computed across the run."""
    objectives = [
        Objective("tput", "max"),
        Objective("lat_ms", "min"),
    ]
    profiler_responses = [
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=100.0,
            perf_unit="tput",
            metrics={"tput": 100.0, "lat_ms": 80.0},
        ),
        ProfilerSummary(
            analysis="ok",
            bottlenecks="none",
            suggestions="none",
            perf_metric=80.0,
            perf_unit="tput",
            metrics={"tput": 80.0, "lat_ms": 50.0},
        ),
    ]
    runner = _make_runner(profiler_responses=profiler_responses)
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=2, children_per_generation=1,
        objectives=objectives, frontier_bias=1.0,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    seed, child = pop.all
    assert seed.metrics == {"tput": 100.0, "lat_ms": 80.0}
    assert child.metrics == {"tput": 80.0, "lat_ms": 50.0}

    # The two individuals trade off — both should be on the frontier.
    front_ids = {i.id for i in pop.frontier(objectives)}
    assert front_ids == {seed.id, child.id}


def test_pareto_addendum_appears_in_profiler_prompt(tmp_path, ref_file):
    """When Pareto mode is on, the profiler system prompt gets an addendum
    explicitly listing the metric keys to emit. The judge stays unaffected."""
    captured_profiler_prompts: list[str] = []

    def _make_runner_with_profiler_capture():
        runner = _make_runner(
            profiler_responses=[
                ProfilerSummary(
                    analysis="ok",
                    bottlenecks="none",
                    suggestions="none",
                    perf_metric=10.0,
                    perf_unit="tok/s",
                    metrics={"tput": 10.0, "lat_ms": 50.0},
                )
            ],
        )
        original = runner.invoke.side_effect

        def spy(*, kind, response_cls, system_prompt="", **kwargs):
            if kind == "profiler":
                captured_profiler_prompts.append(system_prompt)
            return original(
                kind=kind, response_cls=response_cls,
                system_prompt=system_prompt, **kwargs,
            )

        runner.invoke.side_effect = spy
        return runner

    runner = _make_runner_with_profiler_capture()
    objectives = [Objective("tput", "max"), Objective("lat_ms", "min")]
    _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=1, children_per_generation=1,
        objectives=objectives, frontier_bias=1.0,
    )
    assert len(captured_profiler_prompts) == 1
    prompt = captured_profiler_prompts[0]
    assert "Pareto-frontier mode" in prompt
    assert "`tput`" in prompt
    assert "`lat_ms`" in prompt


def test_no_objectives_keeps_metrics_empty_and_legacy_behavior(tmp_path, ref_file):
    """Single-objective mode (no objectives passed) keeps `Individual.metrics`
    empty even if the profiler stub doesn't supply one — preserves the
    pre-Pareto behavior."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=1, children_per_generation=1,
        # Note: no `objectives` kwarg → single-objective mode.
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    assert pop.all[0].metrics == {}


def test_second_child_prompt_includes_parent_block(tmp_path, ref_file):
    """Gen 2's mutator prompt mentions the parent's perf_metric — one of
    the few signals the mutator has to ground its change in fitness."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_loop(
        tmp_path, ref_file, runner,
        max_generations=2, children_per_generation=1,
    )
    assert len(captured) == 2
    gen2_prompt = captured[1]
    assert "Cold start" not in gen2_prompt
    assert "Parent (the implementation you are mutating)" in gen2_prompt
    # The seed's perf_metric (10.0) was emitted by the profiler and should
    # appear in the parent block.
    assert "10.0" in gen2_prompt
