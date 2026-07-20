"""Integration tests for the evolutionary search loop.

Mocks the agent runner so the LLM-driven mutator/judge/profiler return
scripted responses. The real ``_RunContext`` is built on a tmp_path
workspace (so git tracking, snapshots, and population persistence are
exercised end-to-end), but the model + sandbox + agent-runner factories
are patched out — same pattern as ``tests/test_orchestrate.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from vibesys.agents import AgentRunner
from vibesys.loops.evolve.loop import (
    _candidate_runtime_notes,
    _latest_wip_seed,
    _recent_failure_lessons,
    run_evolve_loop,
)
from vibesys.loops.evolve.population import (
    Individual,
    Objective,
    Population,
)
from vibesys.sandbox.run_environment import candidate_modal_app_name
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict

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
        input_path=str(Path(ref_file).parent),
        accuracy_command="uv run python accuracy_checker/checker.py",
        benchmark_command="uv run python benchmark/benchmark.py",
        objective="Maximize tok/s throughput.",
        max_generations=2,
        children_per_generation=1,
        seed=0,
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
    assert "Cold start" in prompt
    assert "Parent (the implementation you are mutating)" not in prompt


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
    assert "Cold start" not in gen1_prompt
    assert "Parent (the implementation you are mutating)" in gen1_prompt
    # The seed's perf_metric (10.0) was emitted by the profiler and should
    # appear in the parent block.
    assert "10.0" in gen1_prompt


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
