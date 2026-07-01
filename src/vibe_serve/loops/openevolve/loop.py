"""OpenEvolve-style outer loop.

Differs from :mod:`vibe_serve.loops.evolve.loop` in exactly one place:
parent + inspiration selection draws from a MAP-Elites
:class:`MapElitesArchive` (cell-uniform sampling), not the flat
fitness-weighted population. Mutator / judge / profiler invocations
and prompt templates are reused verbatim from the evolve loop.

State persistence reuses ``population.json`` (extended in
:class:`Individual` with a ``features`` dict the archive bins on).
"""

from __future__ import annotations

import random

from vibe_serve.agents.progress import CandidateProgress
from vibe_serve.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibe_serve.context import _RunContext
from vibe_serve.loops.evolve.loop import (
    _checkout_commit_tree,
    _current_commit_sha,
    _discard_working_tree,
    _run_judge,
    _run_mutator,
    _run_profiler,
)
from vibe_serve.loops.evolve.population import Individual, Population
from vibe_serve.loops.openevolve.archive import MapElitesArchive, compute_features
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
)
from vibe_serve.schemas import Verdict


def run_openevolve_loop(
    config: dict,
    exp_name: str,
    reference_path: str,
    objective: str,
    *,
    max_iterations: int = 16,
    k_inspirations: int = 3,
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
) -> bool:
    """Run a MAP-Elites driven evolutionary search.

    Each iteration produces one offspring: sample a non-empty archive
    cell uniformly → take its elite as parent → mutate → judge → on
    pass, profile and re-bin into the archive (recomputing the feature
    descriptor from the new ``main.py``). Failed offspring are recorded
    with their judge feedback (no commit) but are not added to the
    archive, mirroring the evolve loop's convention.
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
    ctx.lprint(f"[log] openevolve run: {ctx.run_log_path}")
    ctx.lprint(f"[log] experiment root: {ctx.exp_dir}")
    ctx.lprint(f"[log] objective: {objective.splitlines()[0] if objective else '(empty)'}")

    population_path = ctx.log_dir / "population.json"
    population = Population.load(population_path)
    archive = MapElitesArchive(population)

    rng = random.Random(seed)

    try:
        for iteration in range(1, max_iterations + 1):
            ctx.switch_log_file(f"iter{iteration:03d}")
            candidate_progress = CandidateProgress(iteration, max_iterations, 1, 1)
            ctx.lprint(
                f"\n{'=' * 60}\n  {candidate_progress.label()} — "
                f"archive cells={len(archive)} "
                f"(coverage={archive.coverage():.0%}), "
                f"population={len(population)} (passed={len(population.passed)})\n"
                f"{'=' * 60}\n"
            )

            with ctx.progress(candidate_progress):
                # 1. Pick parent (cell-uniform) + inspirations (other cells).
                parent = archive.sample_cell_elite(rng=rng)
                inspirations = archive.sample_inspirations(
                    parent_id=parent.id if parent else None,
                    k=k_inspirations,
                    rng=rng,
                )
                is_cold_start = parent is None

                # 2. Materialize parent's tree (cold start leaves the framework-seeded
                # workspace untouched).
                if parent is not None and parent.commit:
                    if not _checkout_commit_tree(ctx, parent.commit):
                        ctx.lprint(
                            f"[warn] could not check out parent {parent.id} "
                            f"(commit {parent.commit[:8]}); skipping cand"
                        )
                        continue

                ctx.lprint(
                    f"parent={'COLD-START' if parent is None else f'#{parent.id} (perf={parent.perf_metric}, cell={parent.features})'}; "
                    f"inspirations={[i.id for i in inspirations]}"
                )

                # 3. Mutator edits the workspace. Reuses evolve's prompt; the
                # mutator doesn't need to know which selection scheme picked
                # the parent — only the parent + peers + cold-start flag.
                ctx.reselect_gpu()
                mutator = _run_mutator(
                    ctx,
                    generation=iteration,
                    child_idx=1,
                    objective=objective,
                    parent=parent,
                    inspirations=inspirations,
                    modality=modality,
                    is_cold_start=is_cold_start,
                )

                # 4. Judge.
                ctx.reselect_gpu()
                verdict = _run_judge(
                    ctx,
                    generation=iteration,
                    child_idx=1,
                    modality=modality,
                    objective=objective,
                    pass_criteria=pass_criteria,
                )

                if verdict.verdict != Verdict.PASS:
                    failed = Individual(
                        id=population.next_id(),
                        generation=iteration,
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
                        f"[Round {iteration}] Cand {failed.id} FAILED — "
                        f"feedback: {(verdict.feedback or '').splitlines()[0][:120]}"
                    )
                    continue

                # 5. Profile the offspring to get its fitness.
                ctx.reselect_gpu()
                summary = _run_profiler(
                    ctx,
                    generation=iteration,
                    child_idx=1,
                    modality=modality,
                    objective=objective,
                )

                # 6. Compute behavioral features from the post-mutation
                # workspace (BEFORE commit so the feature read mirrors what
                # the judge/profiler just evaluated). Then commit + record.
                features = compute_features(ctx.workspace)
                ctx.snapshot_workspace(f"iter-{iteration}-cand-1")
                commit = _current_commit_sha(ctx)
                child = Individual(
                    id=population.next_id(),
                    generation=iteration,
                    parent_id=parent.id if parent else None,
                    inspiration_ids=[i.id for i in inspirations],
                    commit=commit,
                    perf_metric=summary.perf_metric if summary else None,
                    perf_unit=summary.perf_unit if summary else None,
                    metrics=dict(summary.metrics) if summary and summary.metrics else {},
                    passed=True,
                    summary=mutator.summary,
                    feedback=verdict.feedback,
                    features=features,
                )
                population.add(child)
                population.save(population_path)

                elite_marker = ""
                cell_key = tuple(
                    features.get(k, 0) for k in ("code_size_bucket", "technique_bucket")
                )
                elites = archive.cells()
                if elites.get(cell_key) is child:
                    elite_marker = " (NEW ELITE)"
                ctx.lprint(
                    f"[Round {iteration}] Cand {child.id} PASSED — "
                    f"perf={child.perf_metric} {child.perf_unit or ''} "
                    f"cell={features}{elite_marker} "
                    f"(parent={parent.id if parent else 'cold'})"
                )

        best = population.best()
        if best is not None:
            ctx.lprint(
                f"\nFinal scalar-best: individual #{best.id} "
                f"perf={best.perf_metric} {best.perf_unit or ''} "
                f"(commit {best.commit[:8] if best.commit else 'n/a'}, "
                f"cell={best.features})"
            )
        else:
            ctx.lprint("\nNo passing individual produced. Inspect logs.")

        ctx.lprint(
            f"\nFinal archive: {len(archive)} cells filled ({archive.coverage():.0%} coverage)"
        )
        return True
    except KeyboardInterrupt:
        ctx.lprint("[openevolve] interrupted; population preserved.")
        return False
    except Exception as exc:
        ctx.lprint(f"[openevolve] aborted with: {exc}")
        return False
    finally:
        ctx.close()
