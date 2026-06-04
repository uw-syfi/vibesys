"""LLM-driven evolutionary search loop.

Each *generation* produces ``children_per_generation`` offspring. For
every offspring:

  1. Sample a parent from the passed-population, weighted by perf_metric.
  2. Sample a small set of peer "inspirations" so the mutator sees
     diverse strategies, not just the current best.
  3. Check the workspace out to the parent's commit (or, on cold start,
     leave the workspace as the framework seeded it).
  4. Run the *Mutator* agent (an LLM acting as the mutation operator) to
     edit code in place.
  5. Run the *Judge* on the result.
  6. If pass, profile to obtain ``perf_metric``. Commit the workspace and
     record an Individual. Else: discard the dirty tree, record a failed
     Individual carrying the judge feedback so future mutators can learn.

Cold start (empty population) bypasses parent selection: the framework
asks the mutator to write the first working server from the reference,
just like the cold-start round in ``orchestrate``. Once that first
individual passes, evolution proper begins on round 2.

The loop intentionally does NOT have an early-stop signal — generations
run for the full ``max_generations`` budget. Termination decisions are
left to the user.
"""

from __future__ import annotations

import random
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from vibe_serve.config import Config
from vibe_serve.constants import ComputeBackend, DEFAULT_COMPUTE_BACKEND
from vibe_serve.context import _RunContext
from vibe_serve.loops.evolve.population import (
    Individual,
    Objective,
    Population,
)
from vibe_serve.schemas import MutatorResponse
from vibe_serve.schemas import ProfilerSummary
from vibe_serve.loops.profiler import invoke_profiler
from vibe_serve.schemas import JudgeResponse, Verdict
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_AGENT_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "agent" / "templates"
)

# Templates here include the orchestrate loop's modality fragments (e.g.
# `_modality/text_generation/implementer.j2`) and reuse its profiler
# prompts verbatim. A Jinja env with both search paths lets us keep the
# evolutionary-specific top-level templates here while leaning on the
# orchestrate package for the bits that don't need to diverge.
_jinja_env = Environment(
    loader=FileSystemLoader([str(_TEMPLATE_DIR), str(_AGENT_TEMPLATE_DIR)]),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(name: str, **kwargs) -> str:
    return _jinja_env.get_template(name).render(**kwargs)


# ---------------------------------------------------------------------------
# Git checkout helpers (parent materialization + dirty-tree discard)
# ---------------------------------------------------------------------------


def _checkout_commit_tree(ctx: _RunContext, commit: str) -> bool:
    """Materialize *commit*'s tree into the working directory.

    Uses ``git checkout <sha> -- .`` so HEAD stays where it is and the
    next ``git commit`` produces a new child commit (rather than
    rewriting history). Untracked files left over from a prior failed
    attempt are removed via ``git clean -fd``.
    """
    try:
        ctx._git_run(["git", "checkout", commit, "--", "."])
        ctx._git_run(["git", "clean", "-fd"], check=False)
        return True
    except Exception as exc:
        ctx.lprint(f"[warn] git checkout {commit[:8]} failed: {exc}")
        return False


def _discard_working_tree(ctx: _RunContext) -> None:
    """Drop any uncommitted changes left by a failed mutation attempt."""
    try:
        ctx._git_run(["git", "checkout", "HEAD", "--", "."], check=False)
        ctx._git_run(["git", "clean", "-fd"], check=False)
    except Exception as exc:
        ctx.lprint(f"[warn] discard working tree failed: {exc}")


def _current_commit_sha(ctx: _RunContext) -> str | None:
    try:
        result = ctx._git_run(["git", "rev-parse", "HEAD"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode(errors="replace").strip()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Profiler MCP wiring (reused from orchestrate; kept here to avoid an
# import-time dependency on the orchestrate loop)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


def _run_mutator(
    ctx: _RunContext,
    *,
    generation: int,
    child_idx: int,
    objective: str,
    parent: Individual | None,
    inspirations: list[Individual],
    modality: str,
    is_cold_start: bool,
    objectives: list[Objective] | None = None,
) -> MutatorResponse:
    system_prompt = _render(
        "mutator_prompt.j2",
        reference_path=ctx.ref_name,
        modality=modality,
        objective=objective,
        parent=parent,
        inspirations=inspirations,
        is_cold_start=is_cold_start,
        objectives=objectives,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
    )
    return ctx.invoke(
        kind="implementer",  # mutator reuses the implementer sandbox
        system_prompt=system_prompt,
        user_prompt=(
            "Edit the workspace to produce an offspring of the parent. "
            "Then return one JSON object matching the schema above."
        ),
        response_cls=MutatorResponse,
        fallback_factory=lambda: MutatorResponse(
            summary="Mutator produced no structured response.",
            hypothesis="unknown",
            expected_behavior="unknown",
        ),
        round_label=f"gen-{generation}-child-{child_idx}-mutator",
    )


def _run_judge(
    ctx: _RunContext,
    *,
    generation: int,
    child_idx: int,
    modality: str,
    objective: str,
    pass_criteria: str,
) -> JudgeResponse:
    system_prompt = _render(
        "judge_prompt.j2",
        accuracy_checker_path=ctx.judge_acc_checker_path,
        bench_path=ctx.judge_bench_path,
        pass_criteria=pass_criteria,
        modality=modality,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
    )
    return ctx.invoke(
        kind="judge",
        system_prompt=system_prompt,
        user_prompt=(
            "Review the offspring per the criteria above. Return only "
            "the JSON verdict."
        ),
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        round_label=f"gen-{generation}-child-{child_idx}-judge",
    )


_PARETO_PROFILER_ADDENDUM = """\

## Pareto-frontier mode — emit *all* configured metrics

This run is in Pareto-frontier mode. In addition to the headline `perf_metric` / `perf_unit`, populate the `metrics` field of `ProfilerSummary` with the numeric value of EVERY objective listed below — read each one from the benchmark tool's JSON output, do not derive, do not invert.

Objectives to report (use these exact key names in `metrics`):

{objective_list}

If the benchmark JSON does not contain a field, set its entry to `null` rather than substituting a derived number — the framework will treat the offspring as missing on that axis and exclude it from the frontier (which is correct: an unmeasured axis cannot be compared).
"""


def _format_objectives_for_profiler(objectives: list[Objective]) -> str:
    return "\n".join(
        f"- `{o.name}` ({'maximize' if o.direction == 'max' else 'minimize'})"
        for o in objectives
    )


def _run_profiler(
    ctx: _RunContext,
    *,
    generation: int,
    child_idx: int,
    modality: str,
    objective: str,
    objectives: list[Objective] | None = None,
) -> ProfilerSummary | None:
    template = (
        "profiler_prompt_torch.j2" if ctx.profiler_kind == "torch"
        else "profiler_prompt_nsys.j2"
    )
    base_prompt = _render(
        template,
        bench_path=ctx.profiler_bench_path,
        modality=modality,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
        profile_focus="Measure the headline metric for this candidate; rank top kernel-level bottlenecks.",
    )
    if objectives:
        addendum = _PARETO_PROFILER_ADDENDUM.format(
            objective_list=_format_objectives_for_profiler(objectives),
        )
        system_prompt = base_prompt + addendum
    else:
        system_prompt = base_prompt
    return invoke_profiler(
        ctx,
        system_prompt=system_prompt,
        round_label=f"gen-{generation}-child-{child_idx}-profiler",
        fallback_suggestions="n/a",
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_evolve_loop(
    config: Config,
    exp_name: str,
    reference_path: str,
    objective: str,
    *,
    max_generations: int = 8,
    children_per_generation: int = 2,
    k_top_inspirations: int = 2,
    k_random_inspirations: int = 2,
    selection_temperature: float = 0.5,
    seed: int | None = None,
    pass_criteria: str = (
        "The server starts, /health returns 200, the accuracy checker "
        "passes, and the benchmark sanity step completes. Code must "
        "actually run inference (no schema-synthesizing reward hacks)."
    ),
    existing: bool = False,
    debug: bool = False,
    acc_checker: str | None = None,
    bench: str | None = None,
    nsys_profiler: str | None = None,
    torch_profiler: str | None = None,
    profiler_kind: str = "auto",
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str = "text_generation",
    objectives: list[Objective] | None = None,
    frontier_bias: float = 0.7,
) -> bool:
    """Run an LLM-driven evolutionary search.

    Returns True if the loop completed normally; False on early
    exception / KeyboardInterrupt.

    When ``objectives`` is non-empty the loop runs in **multi-objective
    mode**: parent / inspiration sampling biases toward the Pareto
    frontier with probability ``frontier_bias`` and the profiler is
    expected to populate ``ProfilerSummary.metrics`` with values for
    every objective name. When ``objectives`` is None the loop runs in
    single-objective mode using ``perf_metric`` only — the legacy
    behavior, kept for back-compat.
    """
    run_environment = run_environment or make_run_environment_spec()
    ctx = _RunContext(
        config=config,
        exp_name=exp_name,
        reference_path=reference_path,
        existing=existing,
        debug=debug,
        acc_checker=acc_checker,
        bench=bench,
        nsys_profiler=nsys_profiler,
        torch_profiler=torch_profiler,
        profiler_kind=profiler_kind,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        git_tracking=True,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
    )
    ctx.lprint(f"[log] evolutionary run: {ctx.run_log_path}")
    ctx.lprint(f"[log] experiment root: {ctx.exp_dir}")
    ctx.lprint(f"[log] objective: {objective.splitlines()[0] if objective else '(empty)'}")
    if objectives:
        spec = ", ".join(f"{o.name}({o.direction})" for o in objectives)
        ctx.lprint(f"[log] pareto objectives: [{spec}], frontier_bias={frontier_bias}")
    else:
        ctx.lprint("[log] single-objective mode (no Pareto frontier)")

    population_path = ctx.log_dir / "population.json"
    population = Population.load(population_path)

    rng = random.Random(seed)

    try:
        for generation in range(1, max_generations + 1):
            ctx.switch_log_file(f"gen{generation:03d}")
            ctx.lprint(
                f"\n{'='*60}\n  Generation {generation}/{max_generations} — "
                f"population={len(population)} (passed={len(population.passed)})\n"
                f"{'='*60}\n"
            )

            for child_idx in range(1, children_per_generation + 1):
                ctx.lprint(f"\n--- gen {generation} child {child_idx}/{children_per_generation} ---\n")

                # 1. Pick parent + inspirations.
                parent = population.select_parent(
                    rng=rng,
                    temperature=selection_temperature,
                    objectives=objectives,
                    frontier_bias=frontier_bias,
                )
                inspirations = population.select_inspirations(
                    parent_id=parent.id if parent else None,
                    k_top=k_top_inspirations,
                    k_random=k_random_inspirations,
                    rng=rng,
                    objectives=objectives,
                )
                is_cold_start = parent is None

                # 2. Materialize parent's tree (skip on cold start —
                # _RunContext seeded the workspace from the reference).
                if parent is not None and parent.commit:
                    if not _checkout_commit_tree(ctx, parent.commit):
                        ctx.lprint(
                            f"[warn] could not check out parent {parent.id} "
                            f"(commit {parent.commit[:8]}); skipping child"
                        )
                        continue

                ctx.lprint(
                    f"parent={'COLD-START' if parent is None else f'#{parent.id} (perf={parent.perf_metric})'}; "
                    f"inspirations={[i.id for i in inspirations]}"
                )

                # 3. Mutator edits the workspace.
                ctx.reselect_gpu()
                mutator = _run_mutator(
                    ctx,
                    generation=generation,
                    child_idx=child_idx,
                    objective=objective,
                    parent=parent,
                    inspirations=inspirations,
                    modality=modality,
                    is_cold_start=is_cold_start,
                    objectives=objectives,
                )

                # 4. Judge.
                ctx.reselect_gpu()
                verdict = _run_judge(
                    ctx,
                    generation=generation,
                    child_idx=child_idx,
                    modality=modality,
                    objective=objective,
                    pass_criteria=pass_criteria,
                )

                if verdict.verdict != Verdict.PASS:
                    # Record the failed child (no commit) so its feedback
                    # is visible to future mutators reading the population.
                    failed = Individual(
                        id=population.next_id(),
                        generation=generation,
                        parent_id=parent.id if parent else None,
                        inspiration_ids=[i.id for i in inspirations],
                        commit=None,
                        perf_metric=None,
                        perf_unit=None,
                        passed=False,
                        summary=mutator.summary,
                        feedback=verdict.feedback,
                    )
                    population.add(failed)
                    population.save(population_path)
                    _discard_working_tree(ctx)
                    ctx.lprint(
                        f"[gen {generation}] child {failed.id} FAILED — "
                        f"feedback: {(verdict.feedback or '').splitlines()[0][:120]}"
                    )
                    continue

                # 5. Profile the offspring to get its fitness.
                ctx.reselect_gpu()
                summary = _run_profiler(
                    ctx,
                    generation=generation,
                    child_idx=child_idx,
                    modality=modality,
                    objective=objective,
                    objectives=objectives,
                )

                # 6. Commit + record.
                ctx.snapshot_workspace(
                    f"gen-{generation}-child-{child_idx}"
                )
                commit = _current_commit_sha(ctx)
                child = Individual(
                    id=population.next_id(),
                    generation=generation,
                    parent_id=parent.id if parent else None,
                    inspiration_ids=[i.id for i in inspirations],
                    commit=commit,
                    perf_metric=summary.perf_metric if summary else None,
                    perf_unit=summary.perf_unit if summary else None,
                    metrics=dict(summary.metrics) if summary and summary.metrics else {},
                    passed=True,
                    summary=mutator.summary,
                    feedback=verdict.feedback,
                )
                population.add(child)
                population.save(population_path)
                metrics_repr = (
                    " ".join(f"{k}={v:g}" for k, v in child.metrics.items())
                    if child.metrics
                    else f"{child.perf_metric} {child.perf_unit or ''}"
                )
                ctx.lprint(
                    f"[gen {generation}] child {child.id} PASSED — "
                    f"{metrics_repr} (parent={parent.id if parent else 'cold'})"
                )

        if objectives:
            front = population.frontier(objectives)
            if front:
                ctx.lprint(f"\nFinal Pareto frontier ({len(front)} individuals):")
                for ind in front:
                    metrics_repr = " ".join(
                        f"{o.name}={ind.metrics.get(o.name, 'n/a'):g}"
                        if isinstance(ind.metrics.get(o.name), (int, float))
                        else f"{o.name}=n/a"
                        for o in objectives
                    )
                    ctx.lprint(
                        f"  #{ind.id}: {metrics_repr} "
                        f"(commit {ind.commit[:8] if ind.commit else 'n/a'})"
                    )
            else:
                ctx.lprint("\nFrontier is empty (no individual reported all objective metrics).")

        best = population.best()
        if best is not None:
            ctx.lprint(
                f"\nFinal scalar-best: individual #{best.id} "
                f"perf={best.perf_metric} {best.perf_unit or ''} "
                f"(commit {best.commit[:8] if best.commit else 'n/a'})"
            )
        else:
            ctx.lprint("\nNo passing individual produced. Inspect logs.")
        return True
    except KeyboardInterrupt:
        ctx.lprint("[evolutionary] interrupted; population preserved.")
        return False
    except Exception as exc:
        ctx.lprint(f"[evolutionary] aborted with: {exc}")
        return False
    finally:
        ctx.close()
