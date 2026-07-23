"""LLM-driven evolutionary search loop.

Each *generation* produces ``children_per_generation`` offspring. For
every offspring:

  1. Sample a parent from the passed-population, weighted by perf_metric.
  2. Sample a small set of peer "inspirations" so the mutator sees
     diverse strategies, not just the current best.
  3. Check the workspace out to the parent's commit.
  4. Run the *Mutator* agent (an LLM acting as the mutation operator) to
     edit code in place.
  5. Run the *Judge* on the result.
  6. If pass, profile to obtain ``perf_metric``. Commit the workspace and
     record an Individual. Else: discard the dirty tree, record a failed
     Individual carrying the judge feedback so future mutators can learn.

Before the generation loop, a dedicated *bootstrap* phase
(``_bootstrap_seed``) runs implementer → judge iterations until the first
judge-passing implementation exists, recorded as a generation-0 seed. So
the generation loop always starts from a passing parent and never
cold-starts. The bootstrap phase owns the from-scratch / fix-forward
repair logic; on ``--resume`` it is skipped when a passing individual is
already present.

The loop intentionally does NOT have an early-stop signal — generations
run for the full ``max_generations`` budget. Termination decisions are
left to the user.
"""

from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader

from vibesys.agents.progress import CandidateProgress
from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_candidate_context, create_run_context
from vibesys.domains.base import DomainDefinition, DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.input_manifest import WorkspaceSource
from vibesys.loops.evolve.population import (
    Individual,
    Objective,
    Population,
)
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
    SearchPolicy,
    SearchPolicyName,
    SearchSelection,
    VibeSysSearchPolicy,
)
from vibesys.loops.profiler import invoke_profiler
from vibesys.profilers import ProfilerKind, profiler_definition
from vibesys.run import LoopContext, RepositoryVisibility
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    candidate_modal_app_name,
    make_run_environment_spec,
)
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_AGENT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "agent" / "templates"
_INTERFACE = "inprocess"

# Evolve owns its top-level mutator and judge prompts but reuses the agent
# loop's modality fragments and profiler prompts. Domain role files are rendered
# separately and injected into both sets of neutral templates.
_jinja_env = Environment(
    loader=FileSystemLoader([str(_TEMPLATE_DIR), str(_AGENT_TEMPLATE_DIR)]),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(name: str, **kwargs: object) -> str:
    return _jinja_env.get_template(name).render(**kwargs)


def _domain_render_context(
    ctx: LoopContext, modality: str | None, *, runtime_notes: str | None = None
) -> dict[str, object]:
    """Return the uniform context understood by every domain role file."""
    return {
        "modality": modality,
        "interface": _INTERFACE,
        "reference_path": ctx.ref_name,
        "benchmark_command": ctx.judge_benchmark_command,
        "accuracy_command": ctx.judge_accuracy_command,
        "runtime_notes": (
            runtime_notes if runtime_notes is not None else ctx.run_environment_view.prompt_notes
        ),
    }


# ---------------------------------------------------------------------------
# Git helpers (dirty-tree discard)
# ---------------------------------------------------------------------------


def _discard_working_tree(ctx: LoopContext) -> None:
    """Drop any uncommitted changes left by a failed mutation attempt."""
    try:
        ctx.git.run(["git", "checkout", "HEAD", "--", "."], check=False)
        ctx.git.run(["git", "clean", "-fd"], check=False)
    except Exception as exc:
        ctx.lprint(f"[warn] discard working tree failed: {exc}")


def _candidate_code(ctx: LoopContext, commit: str) -> str:
    """Canonical multi-file patch used as OpenEvolve's program representation."""
    roots = (
        ctx.git.run(
            ["git", "rev-list", "--max-parents=0", "--reverse", commit],
        )
        .stdout.decode(errors="replace")
        .splitlines()
    )
    if not roots:
        raise ValueError(f"cannot resolve workspace baseline for commit {commit}")
    return ctx.git.run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--full-index",
            roots[0],
            commit,
            "--",
            ".",
            ":(exclude)logs/**",
        ]
    ).stdout.decode(errors="replace")


def _teardown_candidate_app(ctx: LoopContext, cand_app: str | None, *, keep: bool) -> None:
    """Release a candidate's per-evaluation deployment once its judge/profiler are done.

    Every candidate deploys its GPU server to its own per-candidate deployment (so the judge
    never reads a prior candidate's cumulative logs). Once evaluation is over that deployment
    is dead weight — nothing reuses it — so hand it back to the run environment to release.
    The environment decides *how* (e.g. Modal stops the app; local envs are a no-op), so the
    loop stays agnostic to the backend.

    No-op when ``keep`` is set (the ``--keep-modal-apps`` opt-out, for post-hoc log
    inspection) or when ``cand_app`` is None (no per-candidate deployment).
    """
    if keep or not cand_app:
        return
    ctx.run_environment.teardown_deployment(cand_app, log=ctx.lprint)


# ---------------------------------------------------------------------------
# Profiler MCP wiring (reused from orchestrate; kept here to avoid an
# import-time dependency on the orchestrate loop)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


def _recent_failure_lessons(
    population: Population, *, limit: int = 3, max_chars: int = 700
) -> list[str]:
    """Distinct feedback from the most-recent failed individuals.

    While the population has no passing parent, every child is a cold start
    that re-writes the server from scratch. Without this memory the search
    repeats the same bug on every seed (e.g. an identical model-init crash),
    burning generations while the population stays empty. Surfacing the recent
    distinct failure feedback lets each new seed avoid traps earlier seeds hit.

    De-duplicates on a normalized prefix so N identical failures collapse to a
    single lesson, and truncates each to keep the prompt bounded.
    """
    seen: set[str] = set()
    lessons: list[str] = []
    for ind in reversed(population.all):  # most recent first
        if ind.passed:
            continue
        fb = (ind.feedback or "").strip()
        if not fb:
            continue
        key = " ".join(fb[:160].lower().split())
        if key in seen:
            continue
        seen.add(key)
        lessons.append(fb if len(fb) <= max_chars else fb[:max_chars].rstrip() + " …")
        if len(lessons) >= limit:
            break
    return lessons


def _latest_wip_seed(population: Population) -> Individual | None:
    """Most-recent failed cold-start seed whose work was snapshotted.

    While the population has no passing parent, each round is a cold start.
    Rather than throw the failed seed away and rebuild from scratch every
    round (which makes the search re-hit the same bug forever, unable to
    bootstrap its first green candidate), we snapshot each failed seed to a
    WIP commit and let the next cold start *repair it in place* — fix-forward
    instead of restart. This returns that most-recent WIP seed so its tree can
    be checked out as the base for the next attempt.

    A WIP seed is a failed individual (``passed=False``) with ``parent_id is
    None`` that nonetheless carries a ``commit`` (its snapshotted tree).
    """
    for ind in reversed(population.all):  # most recent first
        if not ind.passed and ind.parent_id is None and ind.commit:
            return ind
    return None


def _candidate_runtime_notes(
    ctx: LoopContext, generation: int, child_idx: int
) -> tuple[str, str | None]:
    """Runtime notes for one candidate, with a per-candidate Modal app name.

    In Modal mode every candidate must deploy to its own app so the judge
    never reads a prior (broken) candidate's cumulative app logs. We derive a
    ``-g<gen>c<child>`` app name and substitute it for the per-run base name
    throughout the notes (the base name is a unique token, so a plain replace
    swaps every occurrence — App name, endpoint labels, aux-volume prefixes).
    For non-Modal envs the notes are returned unchanged and the app name is
    ``None``.
    """
    base = getattr(ctx.run_environment_view, "modal_app_name", None)
    notes = ctx.run_environment_view.prompt_notes
    if not base:
        return notes, None
    cand_app = candidate_modal_app_name(base, generation, child_idx)
    return notes.replace(base, cand_app), cand_app


def _run_mutator(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    objective: str,
    parent: Individual | None,
    inspirations: list[Individual],
    modality: str | None,
    domain_definition: DomainDefinition,
    is_cold_start: bool,
    objectives: list[Objective] | None = None,
    failed_lessons: list[str] | None = None,
    num_failed_attempts: int = 0,
    repair_seed: bool = False,
    runtime_notes: str | None = None,
) -> MutatorResponse:
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.run_environment_view.prompt_notes
    )
    domain_implementer = render_domain_section(
        domain_definition,
        DomainRole.IMPLEMENTER,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes),
    )
    system_prompt = _render(
        "mutator_prompt.j2",
        reference_path=ctx.ref_name,
        modality=modality,
        objective=objective,
        parent=parent,
        inspirations=inspirations,
        is_cold_start=is_cold_start,
        objectives=objectives,
        interface=_INTERFACE,
        domain_implementer=domain_implementer,
        runtime_notes=prompt_runtime_notes,
        env_kind=ctx.run_environment_view.env_kind,
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        failed_lessons=failed_lessons or [],
        num_failed_attempts=num_failed_attempts,
        repair_seed=repair_seed,
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
        round_label=f"gen-{generation}-cand-{child_idx}-mutator",
    )


def _run_judge(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    modality: str | None,
    domain_definition: DomainDefinition,
    objective: str,
    pass_criteria: str,
    runtime_notes: str | None = None,
) -> JudgeResponse:
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.run_environment_view.prompt_notes
    )
    domain_judge = render_domain_section(
        domain_definition,
        DomainRole.JUDGE,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes),
    )
    system_prompt = _render(
        "judge_prompt.j2",
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        pass_criteria=pass_criteria,
        modality=modality,
        interface=_INTERFACE,
        domain_judge=domain_judge,
        runtime_notes=prompt_runtime_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
    )
    return ctx.invoke(
        kind="judge",
        system_prompt=system_prompt,
        user_prompt=("Review the offspring per the criteria above. Return only the JSON verdict."),
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        round_label=f"gen-{generation}-cand-{child_idx}-judge",
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
        f"- `{o.name}` ({'maximize' if o.direction == 'max' else 'minimize'})" for o in objectives
    )


def _run_profiler(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    modality: str | None,
    domain_definition: DomainDefinition,
    objective: str,
    objectives: list[Objective] | None = None,
    runtime_notes: str | None = None,
) -> ProfilerSummary | None:
    if ctx.profiler_kind is ProfilerKind.NONE:
        return None
    definition = profiler_definition(ctx.profiler_kind)
    template = definition.prompt_template
    prompt_runtime_notes = (
        runtime_notes if runtime_notes is not None else ctx.run_environment_view.prompt_notes
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, runtime_notes=prompt_runtime_notes),
    )
    base_prompt = _render(
        template,
        benchmark_command=ctx.profiler_benchmark_command,
        modality=modality,
        interface=_INTERFACE,
        domain_profiler=domain_profiler,
        runtime_notes=prompt_runtime_notes,
        env_kind=ctx.run_environment_view.env_kind,
        objective=objective,
        profile_focus="Measure the headline metric for this candidate; rank top kernel-level bottlenecks.",
        profiler_support_name=definition.support_name,
        profiler_mcp_name=definition.mcp_name,
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
        round_label=f"gen-{generation}-cand-{child_idx}-profiler",
        fallback_suggestions="n/a",
    )


@dataclass
class _CandidateOutcome:
    """Result of evaluating one candidate against its parent.

    Deliberately carries no ``Population`` state: evaluation runs on a
    per-candidate context (its own workspace/container in parallel mode), while
    id assignment and ``Population`` mutation happen serially in the
    orchestrator via :func:`_record_outcome`. This split is what makes
    candidate evaluation safe to run concurrently.
    """

    passed: bool
    parent_id: int | None
    inspiration_ids: list[int]
    summary: str
    feedback: str | None
    commit: str | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    policy_parent_id: str | None = None
    target_island: int | None = None


def _evaluate_candidate(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    parent: Individual,
    inspirations: list[Individual],
    objective: str,
    objectives: list[Objective] | None,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_modal_apps: bool,
    policy_parent_id: str | None = None,
    target_island: int | None = None,
    isolated_app: bool = False,
) -> _CandidateOutcome:
    """Mutate → judge → (profile → commit) one candidate on ``ctx``.

    Assumes ``ctx``'s workspace is already materialized at the parent commit
    (serial: the caller checked the shared tree out; parallel: the candidate's
    worktree was created at the parent sha). Touches only ``ctx`` — never the
    shared ``Population`` — so distinct contexts can run this concurrently. The
    per-candidate Modal app is always stopped on the way out.

    ``isolated_app`` selects how the candidate's Modal app is named. In serial
    mode all candidates share one context/app, so we derive a per-candidate app
    name by suffixing (``_candidate_runtime_notes``). In parallel mode each
    candidate already has its own sub-context whose session opened a distinct
    app, so we use that app/notes directly with no further suffixing.
    """
    if isolated_app:
        cand_notes = ctx.run_environment_view.prompt_notes
        cand_app = getattr(ctx.run_environment_view, "modal_app_name", None)
    else:
        # In Modal mode this gives the mutator/judge/profiler a candidate-unique
        # app name so the judge never reads a prior candidate's stale app logs.
        cand_notes, cand_app = _candidate_runtime_notes(ctx, generation, child_idx)
    ctx.lprint(
        f"parent=#{parent.id} (perf={parent.perf_metric})"
        + (f" modal-app={cand_app}" if cand_app else "")
        + f"; inspirations={[i.id for i in inspirations]}"
    )

    inspiration_ids = [i.id for i in inspirations]
    try:
        # 1. Mutator edits the workspace, mutating the passing parent.
        ctx.reselect_gpu()
        mutator = _run_mutator(
            ctx,
            generation=generation,
            child_idx=child_idx,
            objective=objective,
            parent=parent,
            inspirations=inspirations,
            modality=modality,
            domain_definition=domain_definition,
            is_cold_start=False,
            objectives=objectives,
            runtime_notes=cand_notes,
        )

        # 2. Judge.
        ctx.reselect_gpu()
        verdict = _run_judge(
            ctx,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            pass_criteria=pass_criteria,
            runtime_notes=cand_notes,
        )

        if verdict.verdict != Verdict.PASS:
            return _CandidateOutcome(
                passed=False,
                parent_id=parent.id,
                inspiration_ids=inspiration_ids,
                summary=mutator.summary,
                feedback=verdict.feedback,
                policy_parent_id=policy_parent_id,
                target_island=target_island,
            )

        # 3. Profile the offspring to get its fitness.
        ctx.reselect_gpu()
        summary = _run_profiler(
            ctx,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            objectives=objectives,
            runtime_notes=cand_notes,
        )

        # 4. Commit the offspring's tree so it can serve as a future parent.
        ctx.snapshot_workspace(f"gen-{generation}-child-{child_idx}")
        return _CandidateOutcome(
            passed=True,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary=mutator.summary,
            feedback=verdict.feedback,
            commit=ctx.git.current_sha(),
            perf_metric=summary.perf_metric if summary else None,
            perf_unit=summary.perf_unit if summary else None,
            metrics=dict(summary.metrics) if summary and summary.metrics else {},
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )
    finally:
        _teardown_candidate_app(ctx, cand_app, keep=keep_modal_apps)


def _record_outcome(
    ctx: LoopContext,
    population: Population,
    population_path: Path,
    outcome: _CandidateOutcome,
    *,
    generation: int,
    search_policy: SearchPolicy,
    objectives: list[Objective] | None,
) -> Individual:
    """Assign an id, add the individual to the population, and persist it.

    Serialized by construction — the orchestrator calls this from a single
    thread after each candidate's evaluation returns, so ``next_id`` and
    ``Population`` mutation never race even when evaluation ran in parallel.
    """
    individual = Individual(
        id=population.next_id(),
        generation=generation,
        parent_id=outcome.parent_id,
        inspiration_ids=outcome.inspiration_ids,
        commit=outcome.commit,
        perf_metric=outcome.perf_metric,
        perf_unit=outcome.perf_unit,
        metrics=dict(outcome.metrics),
        passed=outcome.passed,
        summary=outcome.summary,
        feedback=outcome.feedback or "",
        policy_parent_id=outcome.policy_parent_id,
        policy_target_island=outcome.target_island,
    )
    population.add(individual)
    population.save(population_path)
    if outcome.passed:
        if individual.commit:
            search_policy.record(
                individual,
                code=(
                    _candidate_code(ctx, individual.commit) if search_policy.requires_code else ""
                ),
                policy_parent_id=outcome.policy_parent_id,
                target_island=outcome.target_island,
                objectives=objectives,
            )
        metrics_repr = (
            " ".join(f"{k}={v:g}" for k, v in individual.metrics.items())
            if individual.metrics
            else f"{individual.perf_metric} {individual.perf_unit or ''}"
        )
        ctx.lprint(
            f"[Gen {generation}] Cand {individual.id} PASSED — "
            f"{metrics_repr} (parent={outcome.parent_id})"
        )
    else:
        ctx.lprint(
            f"[Gen {generation}] Cand {individual.id} FAILED — "
            f"feedback: {(outcome.feedback or '').splitlines()[0][:120] if outcome.feedback else ''}"
        )
    return individual


def _plan_candidate(
    ctx: LoopContext,
    population: Population,
    rng: random.Random,
    *,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    objectives: list[Objective] | None,
    frontier_bias: float,
    search_policy: SearchPolicy | None = None,
) -> SearchSelection | None:
    """Select a (parent, inspirations) pair from the current population.

    Reads ``population`` and advances ``rng`` — must be called from a single
    thread (the orchestrator), never inside a worker. Returns ``None`` when no
    passing parent exists yet (the candidate is skipped). Bootstrap guarantees
    a passing gen-0 seed, so a passer normally always exists; ``select_parent``
    only returns ``None`` when no passer has a scalar ``perf_metric`` (e.g.
    profiler disabled), in which case we fall back to the latest passer so the
    loop never cold-starts.
    """
    policy = search_policy or VibeSysSearchPolicy()
    selection = policy.select(
        population,
        rng=rng,
        k_top_inspirations=k_top_inspirations,
        k_random_inspirations=k_random_inspirations,
        selection_temperature=selection_temperature,
        objectives=objectives,
        frontier_bias=frontier_bias,
    )
    if selection is None:
        ctx.lprint("[warn] no passing parent available; skipping candidate")
    return selection


def _run_generation_serial(
    ctx: LoopContext,
    *,
    generation: int,
    max_generations: int,
    children_per_generation: int,
    population: Population,
    population_path: Path,
    rng: random.Random,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    objective: str,
    objectives: list[Objective] | None,
    frontier_bias: float,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_modal_apps: bool,
    search_policy: SearchPolicy,
) -> None:
    """Evaluate a generation's candidates one at a time on the shared context."""
    for child_idx in range(1, children_per_generation + 1):
        candidate_progress = CandidateProgress(
            generation, max_generations, child_idx, children_per_generation
        )
        with ctx.progress(candidate_progress):
            ctx.lprint(f"\n--- {candidate_progress.label()} ---\n")
            plan = _plan_candidate(
                ctx,
                population,
                rng,
                k_top_inspirations=k_top_inspirations,
                k_random_inspirations=k_random_inspirations,
                selection_temperature=selection_temperature,
                objectives=objectives,
                frontier_bias=frontier_bias,
                search_policy=search_policy,
            )
            if plan is None:
                continue
            parent = plan.parent
            inspirations = plan.inspirations
            if parent.commit and not ctx.git.checkout_tree(parent.commit, clean=True):
                ctx.lprint(
                    f"[warn] could not check out parent {parent.id} "
                    f"(commit {parent.commit[:8]}); skipping cand"
                )
                continue

            outcome = _evaluate_candidate(
                ctx,
                generation=generation,
                child_idx=child_idx,
                parent=parent,
                inspirations=inspirations,
                objective=objective,
                objectives=objectives,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                keep_modal_apps=keep_modal_apps,
                policy_parent_id=plan.policy_parent_id,
                target_island=plan.target_island,
            )
            _record_outcome(
                ctx,
                population,
                population_path,
                outcome,
                generation=generation,
                search_policy=search_policy,
                objectives=objectives,
            )
            if not outcome.passed:
                # Dead-end mutation: revert the dirty tree back to the passing
                # parent for the next candidate.
                _discard_working_tree(ctx)


def _evaluate_in_subcontext(
    parent_ctx: LoopContext,
    *,
    config: Config,
    agent_backend: str | None,
    cli_provider: str | None,
    generation: int,
    child_idx: int,
    parent: Individual,
    inspirations: list[Individual],
    objective: str,
    objectives: list[Objective] | None,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_modal_apps: bool,
    policy_parent_id: str | None,
    target_island: int | None,
    worktree_lock: threading.Lock,
) -> _CandidateOutcome:
    """Run one candidate in its own isolated sub-context (worker thread).

    Never raises: setup/evaluation/teardown failures are logged and folded into
    a failed ``_CandidateOutcome`` so one bad candidate can't sink the pool. The
    ``worktree_lock`` serializes ``git worktree add`` (which mutates the shared
    repo's admin area); everything after — the container, agent calls, and the
    candidate's own commit — is fully isolated per worktree.
    """
    inspiration_ids = [i.id for i in inspirations]
    label = f"g{generation}c{child_idx}"
    commit = parent.commit
    if commit is None:
        parent_ctx.lprint(f"[warn] candidate {label} has no parent commit; skipping")
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate has no parent commit",
            feedback="parent individual has no commit to branch from",
        )
    try:
        with worktree_lock:
            subctx = create_candidate_context(
                cast(Any, parent_ctx),
                config=config,
                generation=generation,
                child_idx=child_idx,
                parent_commit=commit,
                agent_backend=agent_backend,
                cli_provider=cli_provider,
            )
    except Exception as exc:
        parent_ctx.lprint(f"[warn] candidate {label} setup failed: {exc}")
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate setup failed",
            feedback=str(exc),
        )
    try:
        return _evaluate_candidate(
            subctx,
            generation=generation,
            child_idx=child_idx,
            parent=parent,
            inspirations=inspirations,
            objective=objective,
            objectives=objectives,
            modality=modality,
            domain_definition=domain_definition,
            pass_criteria=pass_criteria,
            keep_modal_apps=keep_modal_apps,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
            isolated_app=True,
        )
    except Exception as exc:
        parent_ctx.lprint(f"[warn] candidate {label} evaluation raised: {exc}")
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate evaluation raised",
            feedback=str(exc),
        )
    finally:
        try:
            subctx.close()
        except Exception as exc:
            parent_ctx.lprint(f"[warn] candidate {label} teardown failed: {exc}")


def _run_generation_parallel(
    parent_ctx: LoopContext,
    *,
    config: Config,
    agent_backend: str | None,
    cli_provider: str | None,
    max_parallelism: int,
    generation: int,
    children_per_generation: int,
    population: Population,
    population_path: Path,
    rng: random.Random,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    objective: str,
    objectives: list[Objective] | None,
    frontier_bias: float,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_modal_apps: bool,
    search_policy: SearchPolicy,
) -> None:
    """Evaluate a generation's candidates concurrently in isolated sub-contexts.

    Parent/inspiration selection for *all* children happens first, single-
    threaded, from the pre-generation population snapshot (so ``rng`` and
    ``Population`` are never touched from a worker). Candidates then run in a
    bounded pool; results are recorded serially, in deterministic child order,
    after the pool drains — so id assignment and ``Population`` mutation stay on
    one thread.
    """
    plans: list[tuple[int, SearchSelection]] = []
    for child_idx in range(1, children_per_generation + 1):
        plan = _plan_candidate(
            parent_ctx,
            population,
            rng,
            k_top_inspirations=k_top_inspirations,
            k_random_inspirations=k_random_inspirations,
            selection_temperature=selection_temperature,
            objectives=objectives,
            frontier_bias=frontier_bias,
            search_policy=search_policy,
        )
        if plan is None:
            continue
        parent = plan.parent
        if not parent.commit:
            parent_ctx.lprint(
                f"[warn] parent {parent.id} has no commit; cannot isolate "
                f"candidate g{generation}c{child_idx}; skipping"
            )
            continue
        plans.append((child_idx, plan))

    if not plans:
        return

    cap = max(1, min(max_parallelism, len(plans)))
    parent_ctx.lprint(
        f"[parallel] generation {generation}: evaluating {len(plans)} "
        f"candidate(s), up to {cap} concurrently"
    )
    worktree_lock = threading.Lock()
    outcomes: dict[int, _CandidateOutcome] = {}
    with ThreadPoolExecutor(max_workers=cap, thread_name_prefix=f"gen{generation}") as pool:
        futures = {
            pool.submit(
                _evaluate_in_subcontext,
                parent_ctx,
                config=config,
                agent_backend=agent_backend,
                cli_provider=cli_provider,
                generation=generation,
                child_idx=child_idx,
                parent=plan.parent,
                inspirations=plan.inspirations,
                objective=objective,
                objectives=objectives,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                keep_modal_apps=keep_modal_apps,
                policy_parent_id=plan.policy_parent_id,
                target_island=plan.target_island,
                worktree_lock=worktree_lock,
            ): child_idx
            for (child_idx, plan) in plans
        }
        for future in as_completed(futures):
            outcomes[futures[future]] = future.result()

    # Record serially, in child order, on this (single) thread.
    for child_idx in sorted(outcomes):
        _record_outcome(
            parent_ctx,
            population,
            population_path,
            outcomes[child_idx],
            generation=generation,
            search_policy=search_policy,
            objectives=objectives,
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_seed(
    ctx: LoopContext,
    *,
    objective: str,
    objectives: list[Objective] | None,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    max_attempts: int,
    rng: random.Random,
    population: Population,
    population_path: Path,
    search_policy: SearchPolicy,
    keep_modal_apps: bool = False,
) -> Individual | None:
    """Iterate implementer → judge until a first *passing* seed exists.

    Runs BEFORE the generation loop so the search never cold-starts. Attempt 1
    writes a server from scratch; later attempts repair-forward the most-recent
    failed WIP seed (fix-forward, not restart). On PASS: profile, snapshot, and
    record a passing generation-0 ``Individual`` (``parent_id=None``), then
    return it. On FAIL: snapshot the WIP tree and record a failed generation-0
    ``Individual`` so the next attempt can repair it in place. Returns ``None``
    if every attempt fails — the caller aborts the run.

    ``rng`` is accepted for signature parity with the generation loop (bootstrap
    does no parent/inspiration sampling) and forward-compatibility.
    """
    ctx.switch_log_file("bootstrap")
    ctx.lprint(
        f"\n{'=' * 60}\n  Bootstrap — first passing seed "
        f"(up to {max_attempts} attempt(s))\n{'=' * 60}\n"
    )

    for attempt in range(1, max_attempts + 1):
        ctx.lprint(f"\n--- bootstrap attempt {attempt}/{max_attempts} ---\n")

        # Fix-forward from the most-recent failed WIP seed, if one was
        # snapshotted; otherwise the workspace stays as the framework seeded it
        # (the bare reference tree).
        wip_seed = _latest_wip_seed(population)
        if wip_seed is not None and wip_seed.commit:
            if not ctx.git.checkout_tree(wip_seed.commit, clean=True):
                ctx.lprint(
                    f"[warn] could not check out WIP seed {wip_seed.id} "
                    f"(commit {wip_seed.commit[:8]}); starting from reference"
                )
                wip_seed = None

        # Per-candidate Modal app name so a failed attempt's cumulative app logs
        # never poison the next attempt's judge (same isolation the generation
        # loop uses).
        cand_notes, cand_app = _candidate_runtime_notes(ctx, 0, attempt)
        failed_lessons = _recent_failure_lessons(population)
        num_failed_attempts = sum(1 for i in population.all if not i.passed)
        base_desc = "reference" if wip_seed is None else f"repair-seed #{wip_seed.id}"
        ctx.lprint(f"bootstrap base={base_desc}" + (f" modal-app={cand_app}" if cand_app else ""))

        # The attempt deploys its per-candidate Modal app during mutate/judge/
        # profile; stop it once we're done evaluating, on every exit path.
        try:
            # 1. Implementer (the mutator in cold-start / from-scratch mode).
            ctx.reselect_gpu()
            mutator = _run_mutator(
                ctx,
                generation=0,
                child_idx=attempt,
                objective=objective,
                parent=None,
                inspirations=[],
                modality=modality,
                domain_definition=domain_definition,
                is_cold_start=True,
                objectives=objectives,
                failed_lessons=failed_lessons,
                num_failed_attempts=num_failed_attempts,
                repair_seed=wip_seed is not None,
                runtime_notes=cand_notes,
            )

            # 2. Judge.
            ctx.reselect_gpu()
            verdict = _run_judge(
                ctx,
                generation=0,
                child_idx=attempt,
                modality=modality,
                domain_definition=domain_definition,
                objective=objective,
                pass_criteria=pass_criteria,
                runtime_notes=cand_notes,
            )

            if verdict.verdict != Verdict.PASS:
                # Snapshot the failed tree so the next attempt repairs it in place.
                # Only tag a WIP repair-seed when the snapshot actually committed new
                # work (the tree changed); an unedited tree is nothing to fix-forward.
                wip_commit = None
                try:
                    sha_before = ctx.git.current_sha()
                    ctx.snapshot_workspace(f"wip-seed-bootstrap{attempt}")
                    sha_after = ctx.git.current_sha()
                    if sha_after and sha_after != sha_before:
                        wip_commit = sha_after
                except Exception as exc:
                    ctx.lprint(f"[warn] wip-seed snapshot failed: {exc}")
                failed = Individual(
                    id=population.next_id(),
                    generation=0,
                    parent_id=None,
                    inspiration_ids=[],
                    commit=wip_commit,
                    perf_metric=None,
                    perf_unit=None,
                    passed=False,
                    summary=mutator.summary,
                    feedback=verdict.feedback,
                )
                population.add(failed)
                population.save(population_path)
                ctx.lprint(
                    f"[bootstrap {attempt}] FAILED — feedback: "
                    f"{(verdict.feedback or '').splitlines()[0][:120] if verdict.feedback else ''}"
                )
                continue

            # 3. PASS → profile, snapshot, and record the generation-0 seed.
            ctx.reselect_gpu()
            summary = _run_profiler(
                ctx,
                generation=0,
                child_idx=attempt,
                modality=modality,
                domain_definition=domain_definition,
                objective=objective,
                objectives=objectives,
                runtime_notes=cand_notes,
            )
            ctx.snapshot_workspace("gen-0-seed")
            commit = ctx.git.current_sha()
            seed = Individual(
                id=population.next_id(),
                generation=0,
                parent_id=None,
                inspiration_ids=[],
                commit=commit,
                perf_metric=summary.perf_metric if summary else None,
                perf_unit=summary.perf_unit if summary else None,
                metrics=dict(summary.metrics) if summary and summary.metrics else {},
                passed=True,
                summary=mutator.summary,
                feedback=verdict.feedback,
            )
            population.add(seed)
            population.save(population_path)
            if commit:
                search_policy.record(
                    seed,
                    code=_candidate_code(ctx, commit) if search_policy.requires_code else "",
                    policy_parent_id=None,
                    target_island=None,
                    objectives=objectives,
                )
            ctx.lprint(
                f"[bootstrap {attempt}] PASSED — seed #{seed.id} "
                f"perf={seed.perf_metric} {seed.perf_unit or ''} "
                f"(commit {commit[:8] if commit else 'n/a'})"
            )
            return seed
        finally:
            _teardown_candidate_app(ctx, cand_app, keep=keep_modal_apps)

    ctx.lprint(f"[bootstrap] exhausted {max_attempts} attempt(s) without a passing seed.")
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _initialize_search_policy(
    ctx: LoopContext,
    population: Population,
    *,
    requested: SearchPolicyName | str | None,
    seed: int | None,
    config: OpenEvolveSearchConfig | None,
    objectives: list[Objective] | None,
) -> tuple[SearchPolicyName, SearchPolicy]:
    state_dir = ctx.log_dir / "openevolve"
    if requested is None:
        policy_name = (
            SearchPolicyName.OPENEVOLVE
            if config is not None or OpenEvolveSearchPolicy.has_state(state_dir)
            else SearchPolicyName.VIBESYS
        )
    else:
        policy_name = SearchPolicyName(requested)
        if policy_name is SearchPolicyName.VIBESYS and config is not None:
            raise ValueError("OpenEvolve configuration requires the OpenEvolve search policy")
    if policy_name is not SearchPolicyName.OPENEVOLVE:
        return policy_name, VibeSysSearchPolicy()

    policy = OpenEvolveSearchPolicy(
        state_dir=state_dir,
        seed=seed,
        config=config,
        objectives=objectives,
    )
    for individual in population.passed:
        if not individual.commit:
            continue
        policy.record(
            individual,
            code=_candidate_code(ctx, individual.commit),
            policy_parent_id=(
                individual.policy_parent_id
                or (f"vibesys-{individual.parent_id}" if individual.parent_id is not None else None)
            ),
            target_island=individual.policy_target_island,
            objectives=objectives,
        )
    return policy_name, policy


def run_evolve_loop(
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    objective: str,
    *,
    workspace_seed: Path | None = None,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    evaluator_path: Path | None = None,
    max_generations: int = 8,
    children_per_generation: int = 2,
    k_top_inspirations: int = 2,
    k_random_inspirations: int = 2,
    selection_temperature: float = 0.5,
    seed: int | None = None,
    pass_criteria: str = (
        "The candidate obeys the input bundle's contract, the accuracy "
        "command passes, and the benchmark sanity step completes without "
        "modifying evaluator-owned files."
    ),
    existing: bool = False,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str | None = None,
    domain: DomainName | None = None,
    objectives: list[Objective] | None = None,
    frontier_bias: float = 0.7,
    bootstrap_max_attempts: int = 5,
    keep_modal_apps: bool = False,
    max_parallelism: int = 1,
    search_policy: SearchPolicyName | str | None = None,
    openevolve_config: OpenEvolveSearchConfig | None = None,
    remote_repo: str | None = None,
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE,
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
    if domain is None:
        raise ValueError("domain is required; declare [agent].domain in vibesys.input.toml")
    domain_definition = resolve_domain(domain)
    if modality is None and domain_definition.name is DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = run_environment or make_run_environment_spec()
    ctx = create_run_context(
        config=config,
        exp_name=exp_name,
        input_path=input_path,
        accuracy_command=accuracy_command,
        benchmark_command=benchmark_command,
        workspace_seed=workspace_seed,
        workspace_sources=workspace_sources,
        evaluator_path=evaluator_path,
        existing=existing,
        debug=debug,
        profiler_kind=profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        git_tracking=True,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=remote_repo,
        repo_visibility=repo_visibility,
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
    try:
        population = Population.load(population_path)
        policy_name, policy = _initialize_search_policy(
            ctx,
            population,
            requested=search_policy,
            seed=seed,
            config=openevolve_config,
            objectives=objectives,
        )
    except KeyboardInterrupt:
        ctx.lprint("[evolutionary] interrupted during search-policy initialization.")
        ctx.close()
        return False
    except Exception as exc:
        ctx.lprint(f"[evolutionary] search-policy initialization failed: {exc}")
        ctx.close()
        return False
    ctx.lprint(f"[log] search policy: {policy_name.value}")

    rng = random.Random(seed)

    try:
        # Bootstrap phase: guarantee a passing generation-0 seed before the
        # generation loop, so evolution never cold-starts. Skipped when a
        # passing individual already exists (e.g. --resume).
        if not population.passed:
            seed_individual = _bootstrap_seed(
                ctx,
                objective=objective,
                objectives=objectives,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                max_attempts=bootstrap_max_attempts,
                rng=rng,
                population=population,
                population_path=population_path,
                search_policy=policy,
                keep_modal_apps=keep_modal_apps,
            )
            if seed_individual is None:
                ctx.lprint(
                    "[evolutionary] bootstrap could not produce a passing seed in "
                    f"{bootstrap_max_attempts} attempt(s); aborting before the "
                    "generation loop."
                )
                return False

        # Parallelism is Modal-only: host GPU reselection is a no-op there and
        # each candidate deploys to its own Modal app, so isolated sub-contexts
        # (worktree + editor container + agent runner) can run concurrently.
        # Local/Docker backends contend on one physical GPU, so they stay
        # serial regardless of --max-parallelism.
        env_kind = getattr(ctx.run_environment_view, "env_kind", "local")
        parallel = max_parallelism > 1 and env_kind == "modal"
        if max_parallelism > 1 and not parallel:
            ctx.lprint(
                f"[parallel] --max-parallelism={max_parallelism} ignored: parallel "
                f"candidate evaluation requires Modal (env_kind={env_kind}); running serially"
            )

        for generation in range(1, max_generations + 1):
            ctx.switch_log_file(f"gen{generation:03d}")
            ctx.lprint(
                f"\n{'=' * 60}\n  Generation {generation}/{max_generations} — "
                f"population={len(population)} (passed={len(population.passed)})\n"
                f"{'=' * 60}\n"
            )

            if parallel:
                _run_generation_parallel(
                    ctx,
                    config=config,
                    agent_backend=agent_backend,
                    cli_provider=cli_provider,
                    max_parallelism=max_parallelism,
                    generation=generation,
                    children_per_generation=children_per_generation,
                    population=population,
                    population_path=population_path,
                    rng=rng,
                    k_top_inspirations=k_top_inspirations,
                    k_random_inspirations=k_random_inspirations,
                    selection_temperature=selection_temperature,
                    objective=objective,
                    objectives=objectives,
                    frontier_bias=frontier_bias,
                    modality=modality,
                    domain_definition=domain_definition,
                    pass_criteria=pass_criteria,
                    keep_modal_apps=keep_modal_apps,
                    search_policy=policy,
                )
            else:
                _run_generation_serial(
                    ctx,
                    generation=generation,
                    max_generations=max_generations,
                    children_per_generation=children_per_generation,
                    population=population,
                    population_path=population_path,
                    rng=rng,
                    k_top_inspirations=k_top_inspirations,
                    k_random_inspirations=k_random_inspirations,
                    selection_temperature=selection_temperature,
                    objective=objective,
                    objectives=objectives,
                    frontier_bias=frontier_bias,
                    modality=modality,
                    domain_definition=domain_definition,
                    pass_criteria=pass_criteria,
                    keep_modal_apps=keep_modal_apps,
                    search_policy=policy,
                )

            policy.finish_generation(generation)

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
