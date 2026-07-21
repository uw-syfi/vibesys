"""Integration tests for the evolutionary search loop.

Mocks the agent runner so the LLM-driven mutator/judge/profiler return
scripted responses. The real ``_RunContext`` is built on a tmp_path
workspace (so git tracking, snapshots, and population persistence are
exercised end-to-end), but the model + sandbox + agent-runner factories
are patched out — same pattern as ``tests/test_orchestrate.py``.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vibesys.agents import AgentRunner
from vibesys.context import create_run_context
from vibesys.domains.base import DomainName
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.domains.registry import resolve_domain
from vibesys.loops.evolve import loop as evolve_loop
from vibesys.loops.evolve.loop import (
    _candidate_runtime_notes,
    _CandidateOutcome,
    _evaluate_in_subcontext,
    _latest_wip_seed,
    _plan_candidate,
    _recent_failure_lessons,
    _run_generation_parallel,
    _teardown_candidate_app,
    run_evolve_loop,
)
from vibesys.loops.evolve.population import (
    Individual,
    Objective,
    Population,
)
from vibesys.profilers import ProfilerKind
from vibesys.sandbox.run_environment import RunEnvironmentSpec, candidate_modal_app_name
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict

_LLM_SERVING_DOMAIN = resolve_domain(DomainName.LLM_SERVING)

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
    capture_judge_prompts: list[str] | None = None,
    capture_profiler_prompts: list[str] | None = None,
    mutator_writes: bool = False,
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
            # Simulate a real edit so the cold-start snapshot has something to
            # commit (a WIP repair-seed). Without a file change the snapshot is
            # a no-op and no commit is recorded.
            if mutator_writes:
                workspace = kwargs.get("workspace")
                if workspace is not None:
                    (Path(workspace) / f"mutant_{counters['mutator']}.py").write_text(
                        f"# mutant {counters['mutator']}\n"
                    )
            return MutatorResponse(
                summary=f"mutator call {counters['mutator']}",
                hypothesis="should be faster",
                expected_behavior="ok",
            )
        if kind == "judge":
            if capture_judge_prompts is not None:
                capture_judge_prompts.append(system_prompt)
            idx = counters["judge"]
            counters["judge"] += 1
            v = judge_q[idx] if idx < len(judge_q) else "pass"
            return JudgeResponse(
                analysis="ok",
                feedback="" if v == "pass" else "needs work",
                verdict=Verdict.PASS if v == "pass" else Verdict.FAIL,
            )
        if kind == "profiler":
            if capture_profiler_prompts is not None:
                capture_profiler_prompts.append(system_prompt)
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
        input_path=str(Path(ref_file).parent),
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
        objective="Maximize tok/s throughput.",
        max_generations=2,
        children_per_generation=1,
        seed=0,
        domain=DomainName.LLM_SERVING,
    )
    defaults.update(kwargs)
    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.backends.cuda.LocalShellBackend"),
        patch("vibesys.context.build_agent_runner", return_value=runner),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
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
# Bootstrap phase (runs before the generation loop)
# ---------------------------------------------------------------------------
#
# ``max_generations=0`` runs the bootstrap phase only (the generation loop is
# an empty ``range(1, 1)``), which isolates bootstrap behavior. The mock runner
# counts calls globally, so every multi-generation run has one extra gen-0
# bootstrap round (one mutator + one judge + one profiler) in front.


def test_bootstrap_succeeds_first_try(tmp_path, ref_file):
    """Bootstrap produces the first passing implementation as a generation-0
    seed with parent_id=None and a perf_metric from the profiler."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=0,  # bootstrap only
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    seed = pop.all[0]
    assert seed.id == 1
    assert seed.generation == 0
    assert seed.parent_id is None
    assert seed.passed is True
    assert seed.perf_metric == 10.0
    assert seed.perf_unit == "tok/s"
    assert seed.commit  # an actual git SHA was recorded


def test_bootstrap_fails_all_attempts_returns_false(tmp_path, ref_file):
    """When every bootstrap attempt fails the judge, the run aborts before the
    generation loop and returns False. Failed attempts are never profiled, and
    with no mutator edits they record no commit."""
    runner = _make_runner(judge_verdicts=["fail", "fail"])
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=2,
        max_generations=0,
    )
    assert result is False
    assert runner.counters["mutator"] == 2
    assert runner.counters["judge"] == 2
    assert runner.counters["profiler"] == 0  # judged-fail attempts skip profiling

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    for ind in pop.all:
        assert ind.passed is False
        assert ind.generation == 0
        assert ind.commit is None  # no edits → no WIP snapshot
    assert "needs work" in pop.all[0].feedback


def test_bootstrap_failed_attempt_records_wip_seed_commit(tmp_path, ref_file):
    """A failed bootstrap attempt whose mutator actually edited the workspace is
    snapshotted to a WIP commit, so a later attempt can repair it in place."""
    runner = _make_runner(judge_verdicts=["fail"], mutator_writes=True)
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=1,
        max_generations=0,
    )
    assert result is False  # single attempt, failed → no seed

    pop = _load_population(tmp_path)
    assert len(pop) == 1
    failed = pop.all[0]
    assert failed.passed is False
    assert failed.generation == 0
    assert failed.parent_id is None
    assert failed.commit  # WIP snapshot recorded because the tree changed


def test_bootstrap_repairs_wip_seed_across_attempts(tmp_path, ref_file):
    """A second bootstrap attempt fix-forwards from the most-recent WIP seed:
    it checks that commit out and mutates on top, yielding a fresh WIP commit
    distinct from the first."""
    runner = _make_runner(judge_verdicts=["fail", "fail"], mutator_writes=True)
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=2,
        max_generations=0,
    )
    assert result is False

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    first, second = pop.all
    assert first.passed is False and second.passed is False
    assert first.generation == 0 and second.generation == 0
    assert first.parent_id is None and second.parent_id is None
    # Both attempts snapshotted their (distinct) trees.
    assert first.commit and second.commit
    assert first.commit != second.commit


def test_bootstrap_succeeds_after_repair(tmp_path, ref_file):
    """Bootstrap that fails once then passes: the failed attempt is snapshotted,
    the passing attempt repairs it in place and becomes the gen-0 seed. Only the
    passing attempt is profiled."""
    runner = _make_runner(judge_verdicts=["fail", "pass"], mutator_writes=True)
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        bootstrap_max_attempts=3,
        max_generations=0,
    )
    assert result is True
    assert runner.counters["profiler"] == 1  # only the passing attempt profiled

    pop = _load_population(tmp_path)
    assert len(pop) == 2
    failed, seed = pop.all
    assert failed.passed is False and failed.commit
    assert seed.passed is True
    assert seed.generation == 0
    assert seed.parent_id is None
    # The passing seed built on the repaired WIP tree → distinct commit.
    assert seed.commit and seed.commit != failed.commit


def test_bootstrap_prompt_uses_cold_start_section(tmp_path, ref_file):
    """The bootstrap attempt sees the cold-start branch of the mutator prompt
    (no parent block)."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=0,
    )
    assert len(captured) == 1
    prompt = captured[0]
    assert "Bootstrap the first passing seed" in prompt
    assert "## Parent" not in prompt
    assert "Model weights are at `/model`" in prompt


def test_evolve_with_preexisting_passing_seed_skips_bootstrap(tmp_path, ref_file):
    """A resumed run whose population already has a passing seed skips the
    bootstrap phase entirely and evolves straight off the seed."""
    # First run: bootstrap-only, creates a passing gen-0 seed with a real commit.
    _invoke_loop(tmp_path, ref_file, _make_runner(), max_generations=0)
    exp_envs = list((tmp_path / "exp_env").iterdir())
    assert len(exp_envs) == 1
    exp_name = exp_envs[0].name

    # Second run resumes that exp dir; bootstrap must NOT be called, and a
    # gen-1 child must be appended off the seed.
    with patch("vibesys.loops.evolve.loop._bootstrap_seed") as spy:
        result = _invoke_loop(
            tmp_path,
            ref_file,
            _make_runner(),
            exp_name=exp_name,
            existing=True,
            max_generations=1,
            children_per_generation=1,
        )
        spy.assert_not_called()
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2  # gen-0 seed + one gen-1 child (same exp dir, resumed)
    seed, child = pop.all
    assert seed.generation == 0 and seed.parent_id is None
    assert child.parent_id == seed.id


# ---------------------------------------------------------------------------
# Multi-generation: parent selection + lineage tracking
# ---------------------------------------------------------------------------


def test_first_generation_uses_bootstrap_seed_as_parent(tmp_path, ref_file):
    """Gen 1's child must be tagged with parent_id pointing at the bootstrap
    seed."""
    runner = _make_runner()
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
    )
    assert result is True

    pop = _load_population(tmp_path)
    assert len(pop) == 2  # bootstrap seed + one gen-1 child
    seed, child = pop.all
    assert seed.generation == 0
    assert seed.parent_id is None
    assert child.parent_id == seed.id
    assert child.passed is True
    # Seed and child were profiled separately; two distinct stub values.
    assert {seed.perf_metric, child.perf_metric} == {10.0, 11.0}


def test_failed_child_excluded_from_future_parent_pool(tmp_path, ref_file):
    """Bootstrap: pass (the seed). Gen 1: fail (no commit, not eligible as
    parent). Gen 2: must still parent off the seed — never off the failed
    Gen 1 child."""
    # Judge order: bootstrap(pass), gen1(fail), gen2(pass).
    runner = _make_runner(judge_verdicts=["pass", "fail", "pass"])
    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=2,
        children_per_generation=1,
    )
    assert result is True
    assert runner.counters["mutator"] == 3
    assert runner.counters["judge"] == 3
    assert runner.counters["profiler"] == 2  # only the passes (seed + gen2)

    pop = _load_population(tmp_path)
    assert len(pop) == 3
    seed, g1, g2 = pop.all
    assert seed.passed is True
    assert g1.passed is False
    assert g1.commit is None
    # The gen-2 child must descend from the seed, NOT from the failed g1
    # (which has no commit and can't be selected).
    assert g2.parent_id == seed.id


# ---------------------------------------------------------------------------
# Mutator prompt content
# ---------------------------------------------------------------------------


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
        tmp_path,
        ref_file,
        runner,
        max_generations=1,  # bootstrap seed + one gen-1 child
        children_per_generation=1,
        objectives=objectives,
        frontier_bias=1.0,
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
                kind=kind,
                response_cls=response_cls,
                system_prompt=system_prompt,
                **kwargs,
            )

        runner.invoke.side_effect = spy
        return runner

    runner = _make_runner_with_profiler_capture()
    objectives = [Objective("tput", "max"), Objective("lat_ms", "min")]
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=0,  # only the bootstrap seed is profiled
        objectives=objectives,
        frontier_bias=1.0,
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
        tmp_path,
        ref_file,
        runner,
        max_generations=0,
        # Note: no `objectives` kwarg → single-objective mode.
    )
    assert result is True
    pop = _load_population(tmp_path)
    assert len(pop) == 1
    assert pop.all[0].metrics == {}


def test_second_child_prompt_includes_parent_block(tmp_path, ref_file):
    """Gen 1's mutator prompt mentions the parent (bootstrap seed) perf_metric —
    one of the few signals the mutator has to ground its change in fitness."""
    captured: list[str] = []
    runner = _make_runner(capture_mutator_prompts=captured)
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
    )
    assert len(captured) == 2  # bootstrap (cold-start) + gen-1 (parent block)
    gen1_prompt = captured[1]
    assert "Bootstrap the first passing seed" not in gen1_prompt
    assert "## Parent" in gen1_prompt
    # The seed's perf_metric (10.0) was emitted by the profiler and should
    # appear in the parent block.
    assert "10.0" in gen1_prompt


def test_generic_domain_prompts_exclude_llm_serving_contracts(tmp_path, ref_file):
    """The evolve loop uses registered domain sections instead of baking the
    LLM-serving contract into its mutator and judge base prompts."""
    mutator_prompts: list[str] = []
    judge_prompts: list[str] = []
    runner = _make_runner(
        capture_mutator_prompts=mutator_prompts,
        capture_judge_prompts=judge_prompts,
    )

    result = _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        domain=DomainName.GENERIC,
        modality=None,
        profiler_kind=ProfilerKind.NONE,
        max_generations=0,
    )

    assert result is True
    assert len(mutator_prompts) == len(judge_prompts) == 1
    combined = "\n".join(mutator_prompts + judge_prompts)
    assert "uv run python accuracy_checker/checker.py" in combined
    assert "uv run python benchmark/benchmark.py" in combined
    assert "Model weights are at `/model`" not in combined
    assert "serving-systems" not in combined
    assert "/health" not in combined
    assert "OpenAI-compatible" not in combined


# ---------------------------------------------------------------------------
# Helper units: failure lessons, WIP-seed lookup, per-candidate Modal app
# ---------------------------------------------------------------------------


def _ind(id_, *, passed=False, parent_id=None, commit=None, feedback=""):
    return Individual(
        id=id_,
        generation=1,
        parent_id=parent_id,
        passed=passed,
        commit=commit,
        feedback=feedback,
    )


def test_recent_failure_lessons_dedupes_and_orders_most_recent_first():
    pop = Population()
    pop.add(_ind(1, feedback="crash: CUDA out of memory"))
    pop.add(_ind(2, feedback="crash: CUDA out of memory"))  # duplicate → collapsed
    pop.add(_ind(3, feedback="server never bound to port"))
    pop.add(_ind(4, passed=True, feedback="ignored because it passed"))

    lessons = _recent_failure_lessons(pop, limit=3)
    assert lessons == ["server never bound to port", "crash: CUDA out of memory"]


def test_recent_failure_lessons_truncates_long_feedback():
    pop = Population()
    pop.add(_ind(1, feedback="x" * 5000))
    (lesson,) = _recent_failure_lessons(pop, limit=1, max_chars=100)
    assert lesson.endswith("…")
    assert len(lesson) <= 102  # 100 chars + space + ellipsis


def test_latest_wip_seed_returns_most_recent_failed_seed_with_commit():
    pop = Population()
    pop.add(_ind(1, commit="aaa"))  # failed cold-start seed
    pop.add(_ind(2, commit="bbb"))  # newer failed cold-start seed
    pop.add(_ind(3, passed=True, commit="ccc"))  # passing → not a WIP seed
    pop.add(_ind(4, parent_id=2, commit="ddd"))  # has a parent → not cold-start

    seed = _latest_wip_seed(pop)
    assert seed is not None and seed.id == 2


def test_latest_wip_seed_none_when_no_snapshotted_failure():
    pop = Population()
    pop.add(_ind(1, commit=None))  # failed but never snapshotted
    pop.add(_ind(2, passed=True, commit="ccc"))
    assert _latest_wip_seed(pop) is None


def test_candidate_runtime_notes_substitutes_per_candidate_app_name():
    base = "run-20260720-abcd1234-llama3"
    ctx = SimpleNamespace(
        run_environment_view=SimpleNamespace(
            modal_app_name=base,
            prompt_notes=f"Deploy to Modal app {base}; endpoint {base}-web.",
        )
    )
    notes, app = _candidate_runtime_notes(ctx, generation=3, child_idx=2)
    expected_app = candidate_modal_app_name(base, 3, 2)
    assert app == expected_app
    # Every occurrence of the base name is swapped for the candidate app name
    # (which itself starts with the base + suffix, so the base survives only as
    # that prefix — there is no bare, un-suffixed occurrence left).
    assert notes == f"Deploy to Modal app {expected_app}; endpoint {expected_app}-web."
    assert notes.count(expected_app) == 2


def test_candidate_runtime_notes_noop_without_modal_app():
    notes_in = "Local run; no Modal app."
    ctx = SimpleNamespace(
        run_environment_view=SimpleNamespace(modal_app_name=None, prompt_notes=notes_in)
    )
    notes, app = _candidate_runtime_notes(ctx, generation=1, child_idx=1)
    assert app is None
    assert notes == notes_in


def test_candidate_modal_app_name_suffix_and_length():
    short = candidate_modal_app_name("run-abc", 2, 5)
    assert short == "run-abc-g2c5"

    long_base = "r" * 80
    name = candidate_modal_app_name(long_base, 12, 7)
    assert name.endswith("-g12c7")
    assert len(name) <= 63


# ---------------------------------------------------------------------------
# Candidate-app teardown
# ---------------------------------------------------------------------------


def test_teardown_candidate_app_delegates_to_run_environment():
    """The loop stays backend-agnostic: it hands the app name to the run
    environment, which decides how to release it."""
    run_env = MagicMock()
    ctx = SimpleNamespace(run_environment=run_env, lprint=lambda _: None)

    _teardown_candidate_app(ctx, "vibesys-run-g1c2", keep=False)

    run_env.teardown_deployment.assert_called_once_with("vibesys-run-g1c2", log=ctx.lprint)


def test_teardown_candidate_app_noop_when_kept_or_no_app():
    run_env = MagicMock()
    ctx = SimpleNamespace(run_environment=run_env, lprint=lambda _: None)

    # Opt-out: keep the app for post-hoc inspection.
    _teardown_candidate_app(ctx, "vibesys-run-g1c2", keep=True)
    # No per-candidate deployment (non-Modal env).
    _teardown_candidate_app(ctx, None, keep=False)

    run_env.teardown_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Parallel generation orchestration
# ---------------------------------------------------------------------------


def _passing_seed(commit: str = "seedsha", perf: float = 1.0) -> Individual:
    return Individual(
        id=1,
        generation=0,
        parent_id=None,
        commit=commit,
        perf_metric=perf,
        perf_unit="tok/s",
        metrics={"aggregate_throughput": perf},
        passed=True,
        summary="seed",
    )


def test_plan_candidate_falls_back_to_latest_passer_then_none():
    ctx = SimpleNamespace(lprint=lambda _msg: None)
    rng = random.Random(0)

    # No passers at all → None (candidate skipped).
    empty = Population([])
    assert (
        _plan_candidate(
            ctx,
            empty,
            rng,
            k_top_inspirations=1,
            k_random_inspirations=1,
            selection_temperature=0.5,
            objectives=None,
            frontier_bias=0.7,
        )
        is None
    )

    # A passer exists → returned as parent.
    pop = Population([_passing_seed()])
    plan = _plan_candidate(
        ctx,
        pop,
        rng,
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objectives=None,
        frontier_bias=0.7,
    )
    assert plan is not None
    parent, _inspirations = plan
    assert parent.id == 1


def test_run_generation_parallel_bounds_concurrency_and_records_all(tmp_path, monkeypatch):
    """Candidates run concurrently up to the cap; every result is recorded once,
    in child order, on the orchestrator thread (population never races)."""
    population = Population([_passing_seed()])
    population_path = tmp_path / "population.json"
    logs: list[str] = []
    ctx = SimpleNamespace(lprint=logs.append)

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_eval(parent_ctx, *, generation, child_idx, parent, inspirations, **_kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return _CandidateOutcome(
            passed=True,
            parent_id=parent.id,
            inspiration_ids=[i.id for i in inspirations],
            summary=f"cand-{child_idx}",
            feedback="",
            commit=f"child-{child_idx}",
            perf_metric=float(child_idx),
            perf_unit="tok/s",
            metrics={"aggregate_throughput": float(child_idx)},
        )

    monkeypatch.setattr(evolve_loop, "_evaluate_in_subcontext", fake_eval)

    _run_generation_parallel(
        ctx,
        config={"model": {"name": "m"}},
        agent_backend=None,
        cli_provider=None,
        max_parallelism=2,
        generation=1,
        children_per_generation=5,
        population=population,
        population_path=population_path,
        rng=random.Random(0),
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objective="obj",
        objectives=None,
        frontier_bias=0.7,
        modality="text_generation",
        domain_definition=_LLM_SERVING_DOMAIN,
        pass_criteria="crit",
        keep_modal_apps=False,
    )

    # Cap respected, never exceeded.
    assert peak <= 2
    # All 5 children recorded (ids 2..6) plus the seed.
    assert len(population) == 6
    recorded = [i for i in population.all if i.generation == 1]
    assert len(recorded) == 5
    assert {i.commit for i in recorded} == {f"child-{c}" for c in range(1, 6)}
    assert population_path.exists()


def test_run_generation_parallel_skips_parent_without_commit(tmp_path, monkeypatch):
    """A parent with no commit can't be isolated into a worktree → skipped, not
    dispatched."""
    seed = _passing_seed()
    seed.commit = None  # passer but nothing to branch from
    # ``passed`` requires a commit, so this seed won't be selectable; give the
    # planner a stub that returns it anyway to exercise the guard.
    population = Population([_passing_seed(), seed])
    population_path = tmp_path / "population.json"
    ctx = SimpleNamespace(lprint=lambda _m: None)

    monkeypatch.setattr(
        evolve_loop,
        "_plan_candidate",
        lambda *a, **k: (seed, []),
    )
    called = False

    def fake_eval(*a, **k):
        nonlocal called
        called = True
        return _CandidateOutcome(True, seed.id, [], "s", "")

    monkeypatch.setattr(evolve_loop, "_evaluate_in_subcontext", fake_eval)

    _run_generation_parallel(
        ctx,
        config={"model": {"name": "m"}},
        agent_backend=None,
        cli_provider=None,
        max_parallelism=2,
        generation=1,
        children_per_generation=2,
        population=population,
        population_path=population_path,
        rng=random.Random(0),
        k_top_inspirations=1,
        k_random_inspirations=1,
        selection_temperature=0.5,
        objective="obj",
        objectives=None,
        frontier_bias=0.7,
        modality="text_generation",
        domain_definition=_LLM_SERVING_DOMAIN,
        pass_criteria="crit",
        keep_modal_apps=False,
    )

    assert called is False  # commit-less parent never dispatched
    assert all(i.generation == 0 for i in population.all)  # nothing recorded


# ---------------------------------------------------------------------------
# Isolated sub-context evaluation (worktree + own logger/agent-runner)
# ---------------------------------------------------------------------------


def test_evaluate_in_subcontext_skips_parent_without_commit():
    """A parent with no commit can't seed a worktree — folded into a failed
    outcome without ever building a sub-context."""
    logs: list[str] = []
    parent_ctx = SimpleNamespace(lprint=logs.append)
    parentless = Individual(id=3, generation=1, parent_id=1, commit=None, passed=True, summary="x")

    outcome = _evaluate_in_subcontext(
        parent_ctx,
        config={"model": {"name": "m"}},
        agent_backend=None,
        cli_provider=None,
        generation=2,
        child_idx=1,
        parent=parentless,
        inspirations=[],
        objective="obj",
        objectives=None,
        modality="text_generation",
        domain_definition=_LLM_SERVING_DOMAIN,
        pass_criteria="crit",
        keep_modal_apps=False,
        worktree_lock=threading.Lock(),
    )

    assert outcome.passed is False
    assert outcome.parent_id == 3
    assert "no parent commit" in outcome.summary
    assert any("no parent commit" in line for line in logs)


def test_evaluate_in_subcontext_builds_worktree_and_evaluates(tmp_path, ref_file):
    """End-to-end: a real parent context spawns an isolated candidate sub-context
    (git worktree at the parent commit + its own logger/agent-runner), evaluates
    it, and the offspring commit lands in the parent's shared object store."""
    runner = _make_runner(mutator_writes=True)
    with (
        patch("vibesys.context.build_model", return_value="mock-model"),
        patch("vibesys.backends.cuda.LocalShellBackend"),
        patch("vibesys.context.build_agent_runner", return_value=runner),
        patch("vibesys.context.PROJECT_ROOT", tmp_path),
        create_run_context(
            config={"model": {"name": "claude-sonnet-4-6"}},
            exp_name="test-parallel-subctx",
            input_path=str(Path(ref_file).parent),
            accuracy_command="uv run python accuracy_checker/checker.py",
            benchmark_command="uv run python benchmark/benchmark.py",
            skills_dirs=[],
            run_environment=RunEnvironmentSpec("local"),
            environment_hooks=LLMServingEnvironmentHooks(),
            git_tracking=True,
        ) as parent,
    ):
        base_commit = parent.git.current_sha()
        assert base_commit is not None
        parent_ind = Individual(
            id=1,
            generation=0,
            parent_id=None,
            commit=base_commit,
            perf_metric=1.0,
            perf_unit="tok/s",
            passed=True,
            summary="seed",
        )

        outcome = _evaluate_in_subcontext(
            parent,
            config={"model": {"name": "claude-sonnet-4-6"}},
            agent_backend=None,
            cli_provider=None,
            generation=1,
            child_idx=1,
            parent=parent_ind,
            inspirations=[],
            objective="Maximize tok/s throughput.",
            objectives=None,
            modality="text_generation",
            domain_definition=_LLM_SERVING_DOMAIN,
            pass_criteria="be faster",
            keep_modal_apps=False,
            worktree_lock=threading.Lock(),
        )

        assert outcome.passed is True
        assert outcome.parent_id == 1
        assert outcome.perf_metric == 10.0
        assert outcome.commit and outcome.commit != base_commit
        # The offspring commit is reachable from the parent repo — the worktree
        # shared its object store, so the candidate joins the one lineage.
        assert (
            parent.git.run(["git", "cat-file", "-e", outcome.commit], check=False).returncode == 0
        )
        # The candidate's worktree is torn down when its sub-context closes.
        cand_ws = parent.exp_dir / "candidates" / f"{parent.exp_dir.name}-g1c1" / "workspace"
        assert not cand_ws.exists()


def test_max_parallelism_ignored_on_non_modal_env(tmp_path, ref_file, monkeypatch):
    """--max-parallelism > 1 on a non-Modal env logs a downgrade and runs the
    serial path (parallel orchestrator is never entered)."""
    called = {"parallel": False}
    monkeypatch.setattr(
        evolve_loop,
        "_run_generation_parallel",
        lambda *a, **k: called.__setitem__("parallel", True),
    )
    runner = _make_runner(judge_verdicts=["pass", "pass", "pass"])
    _invoke_loop(
        tmp_path,
        ref_file,
        runner,
        max_generations=1,
        children_per_generation=1,
        max_parallelism=4,  # local env → must downgrade to serial
    )
    assert called["parallel"] is False
    pop = _load_population(tmp_path)
    assert len(pop) == 2  # bootstrap seed + one serial gen-1 candidate


def test_loop_tears_down_candidate_on_pass_and_fail_paths(tmp_path, ref_file):
    """Teardown fires exactly once per candidate on every exit path — the
    bootstrap attempt plus each generation candidate, whether it passes or
    fails the judge."""
    # bootstrap passes (1 attempt), gen-1 candidate passes, gen-2 candidate fails.
    runner = _make_runner(judge_verdicts=["pass", "pass", "fail"])
    with patch("vibesys.loops.evolve.loop._teardown_candidate_app") as teardown:
        _invoke_loop(
            tmp_path,
            ref_file,
            runner,
            max_generations=2,
            children_per_generation=1,
        )

    # 1 bootstrap attempt + 2 generation candidates = 3 teardown calls.
    assert teardown.call_count == 3
