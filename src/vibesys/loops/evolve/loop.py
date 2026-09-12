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
  6. If the judge passes, run the framework-owned accuracy command.
  7. If both pass, profile to obtain ``perf_metric``. Commit the workspace and
     record an Individual. Else: discard the dirty tree, record a failed
     Individual carrying failure feedback so future mutators can learn.

Before the generation loop, a dedicated *bootstrap* phase
(``_bootstrap_seed``) runs implementer → judge → accuracy iterations until
the first framework-verified implementation exists, recorded as a
generation-0 seed. So the generation loop always starts from a passing
parent and never cold-starts. The bootstrap phase owns the from-scratch /
fix-forward repair logic; on ``--resume`` it is skipped when a passing
individual is already present.

The loop intentionally does NOT have an early-stop signal — generations
run for the full ``max_generations`` budget. Termination decisions are
left to the user.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Any, Literal, cast

from jinja2 import Environment, FileSystemLoader

from vibesys.agents.factory import resolve_agent_driver
from vibesys.agents.progress import CandidateProgress
from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_AGENT_BACKEND, DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_candidate_context, create_run_context
from vibesys.domains.base import DomainDefinition, DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.input_manifest import BenchmarkResult, WorkspaceSource  # noqa: TC001  # tracked: #288
from vibesys.loops.evolve.population import (
    Individual,
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
from vibesys.loops.evolve.state import EvolutionStateStore
from vibesys.loops.gates import (
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.loops.profiler import invoke_profiler
from vibesys.profilers import ProfilerKind, profiler_definition
from vibesys.prompts import PROMPTS_DIR
from vibesys.render.sink import output_sink
from vibesys.run import LoopContext, RepositoryVisibility, RunIntegration, RunStateNamespace
from vibesys.run.events import FrameworkSource
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict
from vs_project import EvolveRunConfiguration

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "evolve"
_AGENT_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"
_INTERFACE = "inprocess"

# Shared "no contract declared" default; the dataclass is frozen, so one
# instance is safe as a keyword default.
_NO_BENCHMARK_CONTRACT = BenchmarkContract()

# Evolve owns its top-level mutator and judge prompts but reuses the agent
# loop's modality fragments and profiler prompts. Domain role files are rendered
# separately and injected into both sets of neutral templates.
_jinja_env = Environment(  # noqa: S701  # tracked: #288
    loader=FileSystemLoader([str(_TEMPLATE_DIR), str(_AGENT_TEMPLATE_DIR)]),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(name: str, **kwargs: object) -> str:
    return _jinja_env.get_template(name).render(**kwargs)


def _persist_evolve_state(
    ctx: LoopContext, state_store: EvolutionStateStore, *, label: str
) -> None:
    """Commit the exact durable evolutionary-search state tree."""
    ctx.state.commit(label, state_store.namespace)


def _materialize_selected_candidate(ctx: LoopContext, individual: Individual) -> None:
    """Make one deterministic selected candidate the run branch's final tree."""
    if not individual.commit:
        raise RuntimeError(f"selected individual {individual.id} has no Git commit")  # noqa: TRY003  # tracked: #288
    ctx.git.retain_candidate(f"selected-{individual.id}", individual.commit)
    if not ctx.git.checkout_tree(individual.commit, clean=True):
        raise RuntimeError(  # noqa: TRY003  # tracked: #288
            f"could not materialize selected individual {individual.id} at {individual.commit}"
        )
    ctx.snapshot_workspace(f"evolve: select individual {individual.id}")


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
        "profile_execution": ctx.run_environment_view.profile_execution,
        "workspace_sources": ctx.workspace_sources,
    }


# ---------------------------------------------------------------------------
# Git helpers (dirty-tree discard)
# ---------------------------------------------------------------------------


def _discard_working_tree(ctx: LoopContext) -> None:
    """Drop any uncommitted changes left by a failed mutation attempt."""
    try:
        if not ctx.git.checkout_tree("HEAD", clean=True):
            output_sink().framework_warning(
                "discard working tree failed",
                source=FrameworkSource.LOOP,
            )
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        output_sink().framework_warning(
            "discard working tree failed",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )


def _candidate_code(ctx: LoopContext, commit: str) -> str:
    """Canonical multi-file patch used as OpenEvolve's program representation."""
    return ctx.git.candidate_patch(commit)


def _teardown_candidate_deployment(ctx: LoopContext, deployment: str | None, *, keep: bool) -> None:
    """Release a candidate's per-evaluation deployment once its judge/profiler are done.

    An environment may isolate candidate runtime state in a named deployment so
    the judge never reads a prior candidate's cumulative logs. Once evaluation
    is over, hand that deployment back to the run environment. The adapter owns
    the release mechanism; the loop remains backend-agnostic.

    No-op when ``keep`` is set for post-hoc inspection or when ``deployment`` is
    None (the selected environment has no per-candidate deployment).
    """
    if keep or not deployment:
        return
    ctx.run_environment.teardown_deployment(deployment, log=ctx.lprint)


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
    """Return runtime notes scoped to one environment-owned deployment.

    Environments with named deployment isolation derive the concrete candidate
    name and encode it in their runtime notes. Environments without that
    capability return their notes unchanged.
    """
    runtime = ctx.run_environment.candidate_runtime(ctx.run_environment_view, generation, child_idx)
    return runtime.prompt_notes, runtime.deployment_name


def _run_mutator(  # noqa: PLR0913  # tracked: #288
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
    space: MetricSpace,
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
        space=space,
        interface=_INTERFACE,
        domain_implementer=domain_implementer,
        runtime_notes=prompt_runtime_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
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


def _run_judge(  # noqa: PLR0913  # tracked: #288
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
        profile_execution=ctx.run_environment_view.profile_execution,
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


def _format_objectives_for_profiler(objectives: Sequence[Objective]) -> str:
    return "\n".join(
        f"- `{o.name}` ({'maximize' if o.direction == 'max' else 'minimize'})" for o in objectives
    )


def _run_profiler(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    modality: str | None,
    domain_definition: DomainDefinition,
    objective: str,
    space: MetricSpace,
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
        profile_execution=ctx.run_environment_view.profile_execution,
        objective=objective,
        profile_focus="Measure the headline metric for this candidate; rank top kernel-level bottlenecks.",
        profiler_support_name=definition.support_name,
        profiler_mcp_name=definition.mcp_name,
    )
    if space.objectives:
        addendum = _PARETO_PROFILER_ADDENDUM.format(
            objective_list=_format_objectives_for_profiler(space.objectives),
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


def _run_framework_accuracy_gate(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    timeout_seconds: int | None = None,
) -> str | None:
    """Run the immutable accuracy command and return retry feedback on failure."""
    result = run_accuracy_gate(
        ctx,
        process_id=f"evolve-accuracy-{generation}-{child_idx}",
        timeout_seconds=framework_command_timeout(ctx, timeout_seconds),
        round_label=f"gen-{generation}-cand-{child_idx}",
    )
    return result.feedback


def _run_framework_benchmark_gate(
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    contract: BenchmarkContract,
    space: MetricSpace,
) -> BenchmarkGateResult:
    """Run the declared trusted benchmark result contract for one candidate.

    The gate publishes the same typed ``gate_started``/``gate_finished``
    events the agent loop publishes, so a client watching an evolve run sees
    the framework's measurement of a candidate rather than only the agent
    transcripts around it.
    """
    return run_benchmark_gate(
        ctx,
        result_spec=contract.result_spec,
        result_protocol=contract.result_protocol,
        objectives=space.objectives,
        process_id=f"evolve-benchmark-{generation}-{child_idx}",
        output_slug=f"gen{generation}-cand{child_idx}",
        timeout_seconds=framework_command_timeout(ctx, contract.timeout_seconds),
        round_label=f"gen-{generation}-cand-{child_idx}",
    )


def _run_candidate_gates(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    contract: BenchmarkContract,
    space: MetricSpace,
    accuracy_timeout_seconds: int | None,
) -> tuple[str | None, FrameworkBenchmarkOutcome | None]:
    """Run the accuracy gate, then the benchmark contract when one is declared.

    Returns ``(failure_feedback, benchmark)``. ``failure_feedback`` is ``None``
    when every gate passed; ``benchmark`` is set only when a declared contract
    ran and passed, and it carries the trusted measurement.
    """
    failure_feedback = _run_framework_accuracy_gate(
        ctx,
        generation=generation,
        child_idx=child_idx,
        timeout_seconds=accuracy_timeout_seconds,
    )
    if failure_feedback is not None or not contract.declared:
        return failure_feedback, None
    gate = _run_framework_benchmark_gate(
        ctx,
        generation=generation,
        child_idx=child_idx,
        contract=contract,
        space=space,
    )
    if not gate.passed:
        return gate.outcome.feedback, None
    return None, gate.outcome


def _candidate_fitness(
    summary: ProfilerSummary | None,
    benchmark: FrameworkBenchmarkOutcome | None,
) -> tuple[float | None, str | None, dict[str, float]]:
    """Resolve a candidate's recorded fitness: trusted benchmark over profiler.

    A declared benchmark result contract owns the axes it measures; the
    profiler's self-report owns the rest. The trusted row is therefore merged
    *over* the profiler's row rather than replacing it: the scalar contract
    reports one number, so on a two-axis task replacing the row would leave
    every individual incomplete on the second axis and
    :meth:`Population.frontier` -- which keeps only individuals carrying a
    value for every configured axis -- would return nothing.

    The unit comes from the evaluator's own declaration when the result
    protocol supplies one; ``objectives.toml`` names axes but does not say
    what they are measured in, so the profiler's unit is the fallback.
    """
    metrics = dict(summary.metrics) if summary and summary.metrics else {}
    if (
        benchmark is not None
        and benchmark.metric_name is not None
        and benchmark.metric_value is not None
    ):
        trusted = (
            dict(benchmark.row)
            if benchmark.row
            else {benchmark.metric_name: benchmark.metric_value}
        )
        return (
            benchmark.metric_value,
            benchmark.metric_unit or (summary.perf_unit if summary else None),
            metrics | trusted,
        )
    return (
        summary.perf_metric if summary else None,
        summary.perf_unit if summary else None,
        metrics,
    )


def _evaluate_candidate(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    generation: int,
    child_idx: int,
    parent: Individual,
    inspirations: list[Individual],
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    policy_parent_id: str | None = None,
    target_island: int | None = None,
    isolated_deployment: bool = False,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
) -> _CandidateOutcome:
    """Mutate → judge → accuracy gate → benchmark gate → (profile → commit) one candidate.

    Assumes ``ctx``'s workspace is already materialized at the parent commit
    (serial: the caller checked the shared tree out; parallel: the candidate's
    worktree was created at the parent sha). Touches only ``ctx`` — never the
    shared ``Population`` — so distinct contexts can run this concurrently. The
    per-candidate deployment is always stopped on the way out.

    ``isolated_deployment`` selects whether the candidate already owns a distinct
    environment deployment. In serial mode the environment derives a
    per-candidate name. In parallel mode each candidate's sub-context already
    owns a distinct deployment, so no further suffixing is needed.
    """
    if isolated_deployment:
        cand_notes = ctx.run_environment_view.prompt_notes
        cand_deployment = ctx.run_environment_view.deployment_namespace
    else:
        # Give the mutator/judge/profiler an environment-owned deployment name
        # so a prior candidate's stale state cannot leak into this evaluation.
        cand_notes, cand_deployment = _candidate_runtime_notes(ctx, generation, child_idx)
    ctx.lprint(
        f"parent=#{parent.id} (perf={parent.perf_metric})"
        + (f" deployment={cand_deployment}" if cand_deployment else "")
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
            space=space,
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

        # 3. Framework-owned accuracy gate, then the benchmark result contract
        # when one is declared. The LLM judge cannot waive either. The
        # contract's trusted measurement, not the profiler agent's self-report,
        # is the candidate's fitness, and it runs before the profiler so a
        # failing benchmark short-circuits the expensive diagnostic pass.
        gate_feedback, benchmark = _run_candidate_gates(
            ctx,
            generation=generation,
            child_idx=child_idx,
            contract=benchmark_contract,
            space=space,
            accuracy_timeout_seconds=accuracy_timeout_seconds,
        )
        if gate_feedback is not None:
            return _CandidateOutcome(
                passed=False,
                parent_id=parent.id,
                inspiration_ids=inspiration_ids,
                summary=mutator.summary,
                feedback=gate_feedback,
                policy_parent_id=policy_parent_id,
                target_island=target_island,
            )

        # 4. Profile the offspring; diagnostics plus the fitness fallback when
        # no benchmark contract is declared.
        ctx.reselect_gpu()
        summary = _run_profiler(
            ctx,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            space=space,
            runtime_notes=cand_notes,
        )

        perf_metric, perf_unit, metrics = _candidate_fitness(summary, benchmark)

        # 5. Commit the offspring's tree so it can serve as a future parent.
        ctx.snapshot_workspace(f"gen-{generation}-child-{child_idx}")
        return _CandidateOutcome(
            passed=True,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary=mutator.summary,
            feedback=verdict.feedback,
            commit=ctx.git.current_sha(),
            perf_metric=perf_metric,
            perf_unit=perf_unit,
            metrics=metrics,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )
    finally:
        _teardown_candidate_deployment(ctx, cand_deployment, keep=keep_deployments)


def _record_outcome(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    population: Population,
    state_store: EvolutionStateStore,
    outcome: _CandidateOutcome,
    *,
    generation: int,
    search_policy: SearchPolicy,
    space: MetricSpace,
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
    if individual.commit:
        ctx.git.retain_candidate(f"individual-{individual.id}", individual.commit)
    population.add(individual)
    state_store.save_population(population)
    if outcome.passed:
        if individual.commit:
            search_policy.record(
                individual,
                code=(
                    _candidate_code(ctx, individual.commit) if search_policy.requires_code else ""
                ),
                policy_parent_id=outcome.policy_parent_id,
                target_island=outcome.target_island,
                space=space,
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


def _plan_candidate(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    population: Population,
    state_store: EvolutionStateStore,
    rng: random.Random,
    *,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    space: MetricSpace,
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
        space=space,
        frontier_bias=frontier_bias,
    )
    _persist_evolve_state(
        ctx,
        state_store,
        label="evolve: record search selection",
    )
    if selection is None:
        output_sink().framework_warning(
            "no passing parent available; skipping candidate",
            source=FrameworkSource.LOOP,
        )
    return selection


def _run_generation_serial(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    generation: int,
    max_generations: int,
    children_per_generation: int,
    population: Population,
    state_store: EvolutionStateStore,
    rng: random.Random,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    objective: str,
    space: MetricSpace,
    frontier_bias: float,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    search_policy: SearchPolicy,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
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
                state_store,
                rng,
                k_top_inspirations=k_top_inspirations,
                k_random_inspirations=k_random_inspirations,
                selection_temperature=selection_temperature,
                space=space,
                frontier_bias=frontier_bias,
                search_policy=search_policy,
            )
            if plan is None:
                continue
            parent = plan.parent
            inspirations = plan.inspirations
            if parent.commit and not ctx.git.checkout_tree(parent.commit, clean=True):
                output_sink().framework_warning(
                    f"could not check out parent {parent.id} "
                    f"(commit {parent.commit[:8]}); skipping cand",
                    source=FrameworkSource.LOOP,
                )
                continue

            outcome = _evaluate_candidate(
                ctx,
                generation=generation,
                child_idx=child_idx,
                parent=parent,
                inspirations=inspirations,
                objective=objective,
                space=space,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                keep_deployments=keep_deployments,
                policy_parent_id=plan.policy_parent_id,
                target_island=plan.target_island,
                accuracy_timeout_seconds=accuracy_timeout_seconds,
                benchmark_contract=benchmark_contract,
            )
            individual = _record_outcome(
                ctx,
                population,
                state_store,
                outcome,
                generation=generation,
                search_policy=search_policy,
                space=space,
            )
            if not outcome.passed:
                # Dead-end mutation: revert the dirty tree back to the passing
                # parent for the next candidate.
                _discard_working_tree(ctx)
            _persist_evolve_state(
                ctx,
                state_store,
                label=f"evolve: record individual {individual.id}",
            )


def _evaluate_in_subcontext(  # noqa: PLR0913  # tracked: #288
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
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    policy_parent_id: str | None,
    target_island: int | None,
    worktree_lock: threading.Lock,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
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
        output_sink().framework_warning(
            f"candidate {label} has no parent commit; skipping",
            source=FrameworkSource.LOOP,
        )
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
                cast("Any", parent_ctx),
                config=config,
                generation=generation,
                child_idx=child_idx,
                parent_commit=commit,
                agent_backend=agent_backend,
                cli_provider=cli_provider,
            )
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        output_sink().framework_warning(
            f"candidate {label} setup failed",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate setup failed",
            feedback=str(exc),
        )
    try:
        outcome = _evaluate_candidate(
            subctx,
            generation=generation,
            child_idx=child_idx,
            parent=parent,
            inspirations=inspirations,
            objective=objective,
            space=space,
            modality=modality,
            domain_definition=domain_definition,
            pass_criteria=pass_criteria,
            keep_deployments=keep_deployments,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
            isolated_deployment=True,
            accuracy_timeout_seconds=accuracy_timeout_seconds,
            benchmark_contract=benchmark_contract,
        )
        if outcome.commit:
            # Subcontext teardown removes the linked worktree. Retain its
            # detached commit first so durable population state cannot name an
            # object that Git is then free to prune.
            parent_ctx.git.retain_candidate(label, outcome.commit)
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        output_sink().framework_warning(
            f"candidate {label} evaluation raised",
            detail=str(exc),
            source=FrameworkSource.LOOP,
        )
        return _CandidateOutcome(
            passed=False,
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            summary="candidate evaluation raised",
            feedback=str(exc),
        )
    else:
        return outcome
    finally:
        try:
            subctx.close()
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            output_sink().framework_warning(
                f"candidate {label} teardown failed",
                detail=str(exc),
                source=FrameworkSource.LOOP,
            )


def _run_generation_parallel(  # noqa: PLR0913  # tracked: #288
    parent_ctx: LoopContext,
    *,
    config: Config,
    agent_backend: str | None,
    cli_provider: str | None,
    max_parallelism: int,
    generation: int,
    children_per_generation: int,
    population: Population,
    state_store: EvolutionStateStore,
    rng: random.Random,
    k_top_inspirations: int,
    k_random_inspirations: int,
    selection_temperature: float,
    objective: str,
    space: MetricSpace,
    frontier_bias: float,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    keep_deployments: bool,
    search_policy: SearchPolicy,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
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
            state_store,
            rng,
            k_top_inspirations=k_top_inspirations,
            k_random_inspirations=k_random_inspirations,
            selection_temperature=selection_temperature,
            space=space,
            frontier_bias=frontier_bias,
            search_policy=search_policy,
        )
        if plan is None:
            continue
        parent = plan.parent
        if not parent.commit:
            output_sink().framework_warning(
                f"parent {parent.id} has no commit; cannot isolate "
                f"candidate g{generation}c{child_idx}; skipping",
                source=FrameworkSource.LOOP,
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
                space=space,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                keep_deployments=keep_deployments,
                policy_parent_id=plan.policy_parent_id,
                target_island=plan.target_island,
                worktree_lock=worktree_lock,
                accuracy_timeout_seconds=accuracy_timeout_seconds,
                benchmark_contract=benchmark_contract,
            ): child_idx
            for (child_idx, plan) in plans
        }
        for future in as_completed(futures):
            outcomes[futures[future]] = future.result()

    # Record serially, in child order, on this (single) thread.
    for child_idx in sorted(outcomes):
        individual = _record_outcome(
            parent_ctx,
            population,
            state_store,
            outcomes[child_idx],
            generation=generation,
            search_policy=search_policy,
            space=space,
        )
        _persist_evolve_state(
            parent_ctx,
            state_store,
            label=f"evolve: record individual {individual.id}",
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_seed(  # noqa: PLR0913, PLR0915  # tracked: #288
    ctx: LoopContext,
    *,
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    max_attempts: int,
    rng: random.Random,  # noqa: ARG001  # tracked: #288
    population: Population,
    state_store: EvolutionStateStore,
    search_policy: SearchPolicy,
    keep_deployments: bool = False,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
) -> Individual | None:
    """Iterate implementer → judge → accuracy until a first passing seed exists.

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
        if wip_seed is not None and wip_seed.commit:  # noqa: SIM102  # tracked: #288
            if not ctx.git.checkout_tree(wip_seed.commit, clean=True):
                output_sink().framework_warning(
                    f"could not check out WIP seed {wip_seed.id} "
                    f"(commit {wip_seed.commit[:8]}); starting from reference",
                    source=FrameworkSource.LOOP,
                )
                wip_seed = None

        # Use an environment-owned candidate deployment so a failed attempt's
        # cumulative state never poisons the next attempt's judge.
        cand_notes, cand_deployment = _candidate_runtime_notes(ctx, 0, attempt)
        failed_lessons = _recent_failure_lessons(population)
        num_failed_attempts = sum(1 for i in population.all if not i.passed)
        base_desc = "reference" if wip_seed is None else f"repair-seed #{wip_seed.id}"
        ctx.lprint(
            f"bootstrap base={base_desc}"
            + (f" deployment={cand_deployment}" if cand_deployment else "")
        )

        # Stop the attempt's deployment after mutate/judge/profile on every
        # exit path.
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
                space=space,
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

            benchmark = None
            if verdict.verdict == Verdict.PASS:
                failure_feedback, benchmark = _run_candidate_gates(
                    ctx,
                    generation=0,
                    child_idx=attempt,
                    contract=benchmark_contract,
                    space=space,
                    accuracy_timeout_seconds=accuracy_timeout_seconds,
                )
            else:
                failure_feedback = verdict.feedback

            if failure_feedback is not None:
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
                except Exception as exc:  # noqa: BLE001  # tracked: #288
                    output_sink().framework_warning(
                        "wip-seed snapshot failed",
                        detail=str(exc),
                        source=FrameworkSource.LOOP,
                    )
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
                    feedback=failure_feedback,
                )
                population.add(failed)
                state_store.save_population(population)
                _persist_evolve_state(
                    ctx,
                    state_store,
                    label=f"evolve: record failed bootstrap {attempt}",
                )
                ctx.lprint(
                    f"[bootstrap {attempt}] FAILED — feedback: "
                    f"{failure_feedback.splitlines()[0][:120] if failure_feedback else ''}"
                )
                continue

            # 4. Both gates passed → profile and record the generation-0 seed.
            ctx.reselect_gpu()
            summary = _run_profiler(
                ctx,
                generation=0,
                child_idx=attempt,
                modality=modality,
                domain_definition=domain_definition,
                objective=objective,
                space=space,
                runtime_notes=cand_notes,
            )
            ctx.snapshot_workspace("gen-0-seed")
            commit = ctx.git.current_sha()
            seed_perf_metric, seed_perf_unit, seed_metrics = _candidate_fitness(summary, benchmark)
            seed = Individual(
                id=population.next_id(),
                generation=0,
                parent_id=None,
                inspiration_ids=[],
                commit=commit,
                perf_metric=seed_perf_metric,
                perf_unit=seed_perf_unit,
                metrics=seed_metrics,
                passed=True,
                summary=mutator.summary,
                feedback=verdict.feedback,
            )
            population.add(seed)
            state_store.save_population(population)
            if commit:
                search_policy.record(
                    seed,
                    code=_candidate_code(ctx, commit) if search_policy.requires_code else "",
                    policy_parent_id=None,
                    target_island=None,
                    space=space,
                )
                ctx.git.retain_candidate(f"individual-{seed.id}", commit)
            _persist_evolve_state(
                ctx,
                state_store,
                label=f"evolve: record bootstrap seed {seed.id}",
            )
            ctx.lprint(
                f"[bootstrap {attempt}] PASSED — seed #{seed.id} "
                f"perf={seed.perf_metric} {seed.perf_unit or ''} "
                f"(commit {commit[:8] if commit else 'n/a'})"
            )
            return seed
        finally:
            _teardown_candidate_deployment(ctx, cand_deployment, keep=keep_deployments)

    ctx.lprint(f"[bootstrap] exhausted {max_attempts} attempt(s) without a passing seed.")
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _initialize_search_policy(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    population: Population,
    state_store: EvolutionStateStore,
    *,
    requested: SearchPolicyName | str | None,
    seed: int | None,
    config: OpenEvolveSearchConfig | None,
    space: MetricSpace,
) -> tuple[SearchPolicyName, SearchPolicy]:
    state_dir = state_store.namespace.external_directory("openevolve")
    if requested is None:
        policy_name = (
            SearchPolicyName.OPENEVOLVE
            if config is not None or OpenEvolveSearchPolicy.has_state(state_dir)
            else SearchPolicyName.VIBESYS
        )
    else:
        policy_name = SearchPolicyName(requested)
        if policy_name is SearchPolicyName.VIBESYS and config is not None:
            raise ValueError("OpenEvolve configuration requires the OpenEvolve search policy")  # noqa: TRY003  # tracked: #288
    if policy_name is not SearchPolicyName.OPENEVOLVE:
        return policy_name, VibeSysSearchPolicy()

    policy = OpenEvolveSearchPolicy(
        state_dir=state_dir,
        seed=seed,
        config=config,
        space=space,
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
            space=space,
        )
    return policy_name, policy


def run_evolve_loop(  # noqa: C901, PLR0912, PLR0913, PLR0915  # tracked: #288
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    objective: str,
    *,
    runs_dir: Path | None,
    task_name: str | None = None,
    task_root: Path | None = None,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    evaluator_path: Path | None = None,
    evaluator_package_root: Path | None = None,
    accuracy_timeout_seconds: int | None = None,
    benchmark_result: BenchmarkResult | None = None,
    benchmark_result_protocol: Literal[2] | None = None,
    benchmark_timeout_seconds: int | None = None,
    max_generations: int = 8,
    children_per_generation: int = 2,
    k_top_inspirations: int = 2,
    k_random_inspirations: int = 2,
    selection_temperature: float = 0.5,
    seed: int | None = None,
    pass_criteria: str = (
        "The candidate obeys the input bundle's contract, the accuracy "  # noqa: S107  # tracked: #288
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
    space: MetricSpace,
    frontier_bias: float = 0.7,
    bootstrap_max_attempts: int = 5,
    keep_deployments: bool = False,
    max_parallelism: int = 1,
    search_policy: SearchPolicyName | str | None = None,
    openevolve_config: OpenEvolveSearchConfig | None = None,
    remote_repo: str | None = None,
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE,
    integration: RunIntegration | None = None,
) -> bool:
    """Run an LLM-driven evolutionary search.

    Returns True if the loop completed normally; False on early
    exception / KeyboardInterrupt.

    ``space`` is the run's metric space: the objective axes the task declares
    and the relative tolerance below which two readings are indistinguishable.
    It is persisted with the run and is the only thing that decides whether one
    candidate beats another, so selection honors the declared tolerance.

    When the space has axes the loop runs in **multi-objective mode**: parent /
    inspiration sampling biases toward the Pareto frontier with probability
    ``frontier_bias`` and the profiler is expected to populate
    ``ProfilerSummary.metrics`` with values for every objective name. With no
    axes the loop runs in single-objective mode using ``perf_metric`` only —
    the legacy behavior, kept for back-compat.
    """
    if domain is None:
        raise ValueError("domain is required; declare [agent].domain in vibesys.input.toml")  # noqa: TRY003  # tracked: #288
    domain_definition = resolve_domain(domain)
    if modality is None and domain_definition.name is DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = run_environment or make_run_environment_spec()
    normalized_config = as_config(config)
    selected_policy = SearchPolicyName(search_policy).value if search_policy is not None else None
    resolved_agent_backend = (
        agent_backend or normalized_config.agent.backend or DEFAULT_AGENT_BACKEND
    )
    run_configuration = EvolveRunConfiguration(
        outer_loop="evolve",
        run_environment=run_environment_record(run_environment),
        model=normalized_config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(
            resolve_agent_driver(normalized_config) if resolved_agent_backend == "cli" else None
        ),
        cli_provider=cli_provider or normalized_config.agent.cli_provider or "codex",
        cli_timeout=normalized_config.agent.cli_timeout,
        compute_backend=backend.value,
        profiler=profiler_kind.value,
        modality=modality,
        default_reasoning_effort=normalized_config.thinking.level,
        outer_model=normalized_config.agent.outer.model,
        outer_reasoning_effort=normalized_config.agent.outer.reasoning_effort,
        inner_model=normalized_config.agent.inner.model,
        inner_reasoning_effort=normalized_config.agent.inner.reasoning_effort,
        max_generations=max_generations,
        children_per_generation=children_per_generation,
        k_top_inspirations=k_top_inspirations,
        k_random_inspirations=k_random_inspirations,
        selection_temperature=selection_temperature,
        seed=seed,
        search_policy=selected_policy,
        openevolve_population_size=(
            openevolve_config.population_size if openevolve_config is not None else None
        ),
        openevolve_archive_size=(
            openevolve_config.archive_size if openevolve_config is not None else None
        ),
        openevolve_num_islands=(
            openevolve_config.num_islands if openevolve_config is not None else None
        ),
        openevolve_migration_interval=(
            openevolve_config.migration_interval if openevolve_config is not None else None
        ),
        openevolve_migration_rate=(
            openevolve_config.migration_rate if openevolve_config is not None else None
        ),
        frontier_bias=frontier_bias,
        bootstrap_max_attempts=bootstrap_max_attempts,
        keep_deployments=keep_deployments,
        max_parallelism=max_parallelism,
        objectives=tuple(f"{item.name}:{item.direction}" for item in space.objectives),
    )
    benchmark_contract = BenchmarkContract(
        result_spec=benchmark_result,
        result_protocol=benchmark_result_protocol,
        timeout_seconds=benchmark_timeout_seconds,
    )
    ctx = create_run_context(
        config=normalized_config,
        exp_name=exp_name,
        runs_dir=runs_dir,
        input_path=input_path,
        accuracy_command=accuracy_command,
        benchmark_command=benchmark_command,
        task_name=task_name,
        task_root=task_root,
        workspace_sources=workspace_sources,
        evaluator_path=evaluator_path,
        evaluator_package_root=evaluator_package_root,
        benchmark_output_argument=benchmark_contract.output_argument,
        existing=existing,
        debug=debug,
        profiler_kind=profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        project_configuration=run_configuration,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=remote_repo,
        repo_visibility=repo_visibility,
        integration=integration,
    )
    state_store = EvolutionStateStore(ctx.state.portable(RunStateNamespace.EVOLVE))
    try:
        population = state_store.load_population()
        # A resumed run whose task file has been edited selects by the new
        # space from here on; say so rather than letting the change be silent.
        if state_store.load_metric_space() not in {space, MetricSpace()}:
            output_sink().framework_warning(
                "this run recorded a different metric space; the task file "
                f"wins and selection now uses {len(space.objectives)} axes within "
                f"a {space.relative_noise:.0%} tolerance",
                source=FrameworkSource.LOOP,
            )
        # Materialize an empty population too, so a newly initialized run has
        # one complete, inspectable persistence contract from the start. The
        # metric space is written the same way and in the same place: one
        # record of how this run decides that a candidate is better.
        state_store.save_population(population)
        state_store.save_metric_space(space)
        policy_name, policy = _initialize_search_policy(
            ctx,
            population,
            state_store,
            requested=search_policy,
            seed=seed,
            config=openevolve_config,
            space=space,
        )
        _persist_evolve_state(
            ctx,
            state_store,
            label="evolve: initialize search state",
        )
    except KeyboardInterrupt:
        ctx.lprint("[evolutionary] interrupted during search-policy initialization.")
        ctx.close()
        return False
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        ctx.lprint(f"[evolutionary] search-policy initialization failed: {exc}")
        ctx.close()
        return False

    # One event per run, emitted only after the search policy resolves so the
    # payload is complete; a run that dies during initialization emits none.
    pareto_objectives = None
    if space.objectives:
        spec = ", ".join(f"{o.name}({o.direction})" for o in space.objectives)
        pareto_objectives = (
            f"[{spec}], frontier_bias={frontier_bias}, tolerance={space.relative_noise:.0%}"
        )
    output_sink().run_configured(
        run_log_path=str(ctx.run_log_path),
        project_root=str(ctx.project_root),
        objective=objective,
        search_policy=policy_name.value,
        benchmark_contract=benchmark_contract.declared,
        pareto_objectives=pareto_objectives,
    )

    rng = random.Random(seed)  # noqa: S311  # tracked: #288

    try:
        # Bootstrap phase: guarantee a passing generation-0 seed before the
        # generation loop, so evolution never cold-starts. Skipped when a
        # passing individual already exists (e.g. --resume).
        if not population.passed:
            seed_individual = _bootstrap_seed(
                ctx,
                objective=objective,
                space=space,
                modality=modality,
                domain_definition=domain_definition,
                pass_criteria=pass_criteria,
                max_attempts=bootstrap_max_attempts,
                rng=rng,
                population=population,
                state_store=state_store,
                search_policy=policy,
                keep_deployments=keep_deployments,
                accuracy_timeout_seconds=accuracy_timeout_seconds,
                benchmark_contract=benchmark_contract,
            )
            if seed_individual is None:
                ctx.lprint(
                    "[evolutionary] bootstrap could not produce a passing seed in "
                    f"{bootstrap_max_attempts} attempt(s); aborting before the "
                    "generation loop."
                )
                return False

        # The selected run environment declares whether isolated candidate
        # evaluations can execute concurrently. Local single-device backends
        # normally leave this false; remote deployment adapters may enable it.
        env_kind = ctx.run_environment_view.env_kind
        supports_parallel = ctx.run_environment_view.supports_parallel_candidate_evaluation
        parallel = max_parallelism > 1 and supports_parallel
        if max_parallelism > 1 and not parallel:
            ctx.lprint(
                f"[parallel] --max-parallelism={max_parallelism} ignored: parallel "
                "candidate evaluation is unsupported by the selected environment "
                f"(env_kind={env_kind}); running serially"
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
                    state_store=state_store,
                    rng=rng,
                    k_top_inspirations=k_top_inspirations,
                    k_random_inspirations=k_random_inspirations,
                    selection_temperature=selection_temperature,
                    objective=objective,
                    space=space,
                    frontier_bias=frontier_bias,
                    modality=modality,
                    domain_definition=domain_definition,
                    pass_criteria=pass_criteria,
                    keep_deployments=keep_deployments,
                    search_policy=policy,
                    accuracy_timeout_seconds=accuracy_timeout_seconds,
                    benchmark_contract=benchmark_contract,
                )
            else:
                _run_generation_serial(
                    ctx,
                    generation=generation,
                    max_generations=max_generations,
                    children_per_generation=children_per_generation,
                    population=population,
                    state_store=state_store,
                    rng=rng,
                    k_top_inspirations=k_top_inspirations,
                    k_random_inspirations=k_random_inspirations,
                    selection_temperature=selection_temperature,
                    objective=objective,
                    space=space,
                    frontier_bias=frontier_bias,
                    modality=modality,
                    domain_definition=domain_definition,
                    pass_criteria=pass_criteria,
                    keep_deployments=keep_deployments,
                    search_policy=policy,
                    accuracy_timeout_seconds=accuracy_timeout_seconds,
                    benchmark_contract=benchmark_contract,
                )

            policy.finish_generation(generation)
            _persist_evolve_state(
                ctx,
                state_store,
                label=f"evolve: complete generation {generation}",
            )

        if space.objectives:
            front = population.frontier(space)
            if front:
                ctx.lprint(f"\nFinal Pareto frontier ({len(front)} individuals):")
                for ind in front:
                    metrics_repr = " ".join(
                        f"{o.name}={ind.metrics.get(o.name, 'n/a'):g}"
                        if isinstance(ind.metrics.get(o.name), (int, float))
                        else f"{o.name}=n/a"
                        for o in space.objectives
                    )
                    ctx.lprint(
                        f"  #{ind.id}: {metrics_repr} "
                        f"(commit {ind.commit[:8] if ind.commit else 'n/a'})"
                    )
            else:
                ctx.lprint("\nFrontier is empty (no individual reported all objective metrics).")

        best = population.best(space)
        if best is None and population.passed:
            # A profiler-disabled run has no scalar fitness. Prefer the latest
            # passing individual, matching the search-policy fallback.
            best = max(population.passed, key=lambda individual: individual.id)
        if best is not None:
            _materialize_selected_candidate(ctx, best)
            ctx.lprint(
                f"\nFinal scalar-best: individual #{best.id} "
                f"perf={best.perf_metric} {best.perf_unit or ''} "
                f"(commit {best.commit[:8] if best.commit else 'n/a'})"
            )
        else:
            ctx.lprint("\nNo passing individual produced. Inspect logs.")
        return True  # noqa: TRY300  # tracked: #288
    except KeyboardInterrupt:
        ctx.lprint("[evolutionary] interrupted; population preserved.")
        return False
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        ctx.lprint(f"[evolutionary] aborted with: {exc}")
        return False
    finally:
        ctx.close()
