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

from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from jinja2 import Environment, FileSystemLoader

from vibesys.context import create_candidate_context
from vibesys.domains.base import DomainDefinition, DomainRole
from vibesys.domains.rendering import render_domain_section
from vibesys.evaluators.gates import (
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.events import FrameworkSource
from vibesys.loops.evolve.policy_flow import (
    BootstrapAttemptResult,
    CandidateOutcome,
)
from vibesys.loops.evolve.population import (
    Individual,
    Population,
)
from vibesys.loops.evolve.search_policy import (
    OpenEvolveSearchConfig,
    OpenEvolveSearchPolicy,
    SearchPolicy,
    SearchPolicyName,
    VibeSysSearchPolicy,
)
from vibesys.loops.profiler import invoke_profiler
from vibesys.profilers import ProfilerKind, profiler_definition
from vibesys.prompts import PROMPTS_DIR
from vibesys.render.sink import output_sink
from vibesys.schemas import JudgeResponse, MutatorResponse, ProfilerSummary, Verdict

if TYPE_CHECKING:
    import threading

    from vibesys.config import Config
    from vibesys.evaluators.metrics import MetricSpace, Objective
    from vibesys.loops.evolve.state import EvolutionStateStore
    from vibesys.run import LoopContext

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
    ctx.publish_committed_state("evolve", state_store.projection())


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


_CandidateOutcome = CandidateOutcome


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
    """Bind the candidate policy to this context's agents, gates, and workspace."""
    if isolated_deployment:
        cand_notes = ctx.run_environment_view.prompt_notes
        cand_deployment = ctx.run_environment_view.deployment_namespace
    else:
        cand_notes, cand_deployment = _candidate_runtime_notes(ctx, generation, child_idx)
    ctx.lprint(
        f"parent=#{parent.id} (perf={parent.perf_metric})"
        + (f" deployment={cand_deployment}" if cand_deployment else "")
        + f"; inspirations={[i.id for i in inspirations]}"
    )

    def outcome(
        *,
        passed: bool,
        summary: str,
        feedback: str | None,
        commit: str | None = None,
        fitness: tuple[float | None, str | None, dict[str, float]] | None = None,
    ) -> _CandidateOutcome:
        metric, unit, metrics = fitness if fitness is not None else (None, None, {})
        return _CandidateOutcome(
            passed=passed,
            parent_id=parent.id,
            inspiration_ids=[individual.id for individual in inspirations],
            summary=summary,
            feedback=feedback,
            commit=commit,
            perf_metric=metric,
            perf_unit=unit,
            metrics=metrics,
            policy_parent_id=policy_parent_id,
            target_island=target_island,
        )

    try:
        ctx.reselect_gpu()
        response = _run_mutator(
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
            return outcome(passed=False, summary=response.summary, feedback=verdict.feedback)
        gate_feedback, benchmark = _run_candidate_gates(
            ctx,
            generation=generation,
            child_idx=child_idx,
            contract=benchmark_contract,
            space=space,
            accuracy_timeout_seconds=accuracy_timeout_seconds,
        )
        if gate_feedback is not None:
            return outcome(passed=False, summary=response.summary, feedback=gate_feedback)
        ctx.reselect_gpu()
        profile = _run_profiler(
            ctx,
            generation=generation,
            child_idx=child_idx,
            modality=modality,
            domain_definition=domain_definition,
            objective=objective,
            space=space,
            runtime_notes=cand_notes,
        )
        fitness = _candidate_fitness(profile, benchmark)
        ctx.snapshot_workspace(f"gen-{generation}-child-{child_idx}")
        return outcome(
            passed=True,
            summary=response.summary,
            feedback=verdict.feedback,
            commit=ctx.git.current_sha(),
            fitness=fitness,
        )
    finally:
        _teardown_candidate_deployment(ctx, cand_deployment, keep=keep_deployments)


class _LoopSearchEffects:
    """Bind evolve search effects to the current run context and state store."""

    def __init__(self, ctx: LoopContext, state_store: EvolutionStateStore) -> None:
        self.ctx = ctx
        self.state_store = state_store

    def checkpoint(self, label: str) -> None:
        _persist_evolve_state(self.ctx, self.state_store, label=label)

    def save_population(self, population: Population) -> None:
        self.state_store.save_population(population)

    def retain_candidate(self, label: str, commit: str) -> None:
        self.ctx.git.retain_candidate(label, commit)

    def candidate_code(self, commit: str) -> str:
        return _candidate_code(self.ctx, commit)

    def log(self, message: str) -> None:
        self.ctx.lprint(message)

    def warn(self, message: str) -> None:
        output_sink().framework_warning(message, source=FrameworkSource.LOOP)


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


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BootstrapPassingEvidence:
    summary: str
    feedback: str
    benchmark: FrameworkBenchmarkOutcome | None
    runtime_notes: str


@dataclass(slots=True)
class _BootstrapAdapter:
    """Bind one bootstrap attempt to the run's agents, gates, and state."""

    ctx: LoopContext
    objective: str
    space: MetricSpace
    modality: str | None
    domain_definition: DomainDefinition
    pass_criteria: str
    population: Population
    state_store: EvolutionStateStore
    search_policy: SearchPolicy
    keep_deployments: bool
    accuracy_timeout_seconds: int | None
    benchmark_contract: BenchmarkContract

    def attempt(self, number: int, max_attempts: int) -> BootstrapAttemptResult:
        """Run and record one implementer, judge, gate, and profile attempt."""
        ctx = self.ctx
        ctx.lprint(f"\n--- bootstrap attempt {number}/{max_attempts} ---\n")
        wip_seed = self._repair_seed()
        cand_notes, cand_deployment = _candidate_runtime_notes(ctx, 0, number)
        failed_lessons = _recent_failure_lessons(self.population)
        num_failed_attempts = sum(1 for individual in self.population.all if not individual.passed)
        base_desc = "reference" if wip_seed is None else f"repair-seed #{wip_seed.id}"
        ctx.lprint(
            f"bootstrap base={base_desc}"
            + (f" deployment={cand_deployment}" if cand_deployment else "")
        )
        try:
            ctx.reselect_gpu()
            mutator = _run_mutator(
                ctx,
                generation=0,
                child_idx=number,
                objective=self.objective,
                parent=None,
                inspirations=[],
                modality=self.modality,
                domain_definition=self.domain_definition,
                is_cold_start=True,
                space=self.space,
                failed_lessons=failed_lessons,
                num_failed_attempts=num_failed_attempts,
                repair_seed=wip_seed is not None,
                runtime_notes=cand_notes,
            )
            ctx.reselect_gpu()
            verdict = _run_judge(
                ctx,
                generation=0,
                child_idx=number,
                modality=self.modality,
                domain_definition=self.domain_definition,
                objective=self.objective,
                pass_criteria=self.pass_criteria,
                runtime_notes=cand_notes,
            )
            benchmark = None
            if verdict.verdict == Verdict.PASS:
                failure_feedback, benchmark = _run_candidate_gates(
                    ctx,
                    generation=0,
                    child_idx=number,
                    contract=self.benchmark_contract,
                    space=self.space,
                    accuracy_timeout_seconds=self.accuracy_timeout_seconds,
                )
            else:
                failure_feedback = verdict.feedback
            if failure_feedback is not None:
                return self._record_failure(number, mutator.summary, failure_feedback)
            return self._record_seed(
                number,
                _BootstrapPassingEvidence(mutator.summary, verdict.feedback, benchmark, cand_notes),
            )
        finally:
            _teardown_candidate_deployment(ctx, cand_deployment, keep=self.keep_deployments)

    def _repair_seed(self) -> Individual | None:
        """Return the latest WIP seed if its tree can be checked out."""
        wip_seed = _latest_wip_seed(self.population)
        if wip_seed is not None and wip_seed.commit:  # noqa: SIM102  # tracked: #288
            if not self.ctx.git.checkout_tree(wip_seed.commit, clean=True):
                output_sink().framework_warning(
                    f"could not check out WIP seed {wip_seed.id} "
                    f"(commit {wip_seed.commit[:8]}); starting from reference",
                    source=FrameworkSource.LOOP,
                )
                wip_seed = None
        return wip_seed

    def _snapshot_wip(self, number: int) -> str | None:
        """Retain a failed tree only when the snapshot created a new commit."""
        try:
            sha_before = self.ctx.git.current_sha()
            self.ctx.snapshot_workspace(f"wip-seed-bootstrap{number}")
            sha_after = self.ctx.git.current_sha()
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            output_sink().framework_warning(
                "wip-seed snapshot failed",
                detail=str(exc),
                source=FrameworkSource.LOOP,
            )
            return None
        else:
            return sha_after if sha_after and sha_after != sha_before else None

    def _record_failure(self, number: int, summary: str, feedback: str) -> BootstrapAttemptResult:
        """Record one failed attempt and checkpoint its optional WIP seed."""
        failed = Individual(
            id=self.population.next_id(),
            generation=0,
            parent_id=None,
            inspiration_ids=[],
            commit=self._snapshot_wip(number),
            perf_metric=None,
            perf_unit=None,
            passed=False,
            summary=summary,
            feedback=feedback,
        )
        if failed.commit:
            self.ctx.git.retain_candidate(f"wip-seed-{failed.id}", failed.commit)
        self.population.add(failed)
        self.state_store.save_population(self.population)
        return BootstrapAttemptResult(
            seed=None,
            message=(
                f"[bootstrap {number}] FAILED — feedback: "
                f"{feedback.splitlines()[0][:120] if feedback else ''}"
            ),
        )

    def _record_seed(
        self, number: int, evidence: _BootstrapPassingEvidence
    ) -> BootstrapAttemptResult:
        """Profile and checkpoint the first passing generation-zero seed."""
        ctx = self.ctx
        ctx.reselect_gpu()
        profile = _run_profiler(
            ctx,
            generation=0,
            child_idx=number,
            modality=self.modality,
            domain_definition=self.domain_definition,
            objective=self.objective,
            space=self.space,
            runtime_notes=evidence.runtime_notes,
        )
        ctx.snapshot_workspace("gen-0-seed")
        commit = ctx.git.current_sha()
        perf_metric, perf_unit, metrics = _candidate_fitness(profile, evidence.benchmark)
        seed = Individual(
            id=self.population.next_id(),
            generation=0,
            parent_id=None,
            inspiration_ids=[],
            commit=commit,
            perf_metric=perf_metric,
            perf_unit=perf_unit,
            metrics=metrics,
            passed=True,
            summary=evidence.summary,
            feedback=evidence.feedback,
        )
        if commit:
            ctx.git.retain_candidate(f"individual-{seed.id}", commit)
        self.population.add(seed)
        self.state_store.save_population(self.population)
        if commit:
            self.search_policy.record(
                seed,
                code=_candidate_code(ctx, commit) if self.search_policy.requires_code else "",
                policy_parent_id=None,
                target_island=None,
                space=self.space,
            )
        return BootstrapAttemptResult(
            seed=seed,
            message=(
                f"[bootstrap {number}] PASSED — seed #{seed.id} "
                f"perf={seed.perf_metric} {seed.perf_unit or ''} "
                f"(commit {commit[:8] if commit else 'n/a'})"
            ),
        )


def _bootstrap_seed(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    objective: str,
    space: MetricSpace,
    modality: str | None,
    domain_definition: DomainDefinition,
    pass_criteria: str,
    max_attempts: int,
    population: Population,
    state_store: EvolutionStateStore,
    search_policy: SearchPolicy,
    keep_deployments: bool = False,
    accuracy_timeout_seconds: int | None = None,
    benchmark_contract: BenchmarkContract = _NO_BENCHMARK_CONTRACT,
) -> Individual | None:
    """Retry and checkpoint generation-zero attempts until a seed passes."""
    ctx.switch_log_file("bootstrap")
    ctx.lprint(
        f"\n{'=' * 60}\n  Bootstrap — first passing seed "
        f"(up to {max_attempts} attempt(s))\n{'=' * 60}\n"
    )
    bootstrap = _BootstrapAdapter(
        ctx=ctx,
        objective=objective,
        space=space,
        modality=modality,
        domain_definition=domain_definition,
        pass_criteria=pass_criteria,
        population=population,
        state_store=state_store,
        search_policy=search_policy,
        keep_deployments=keep_deployments,
        accuracy_timeout_seconds=accuracy_timeout_seconds,
        benchmark_contract=benchmark_contract,
    )
    for number in range(1, max_attempts + 1):
        result = bootstrap.attempt(number, max_attempts)
        label = (
            f"evolve: record bootstrap seed {result.seed.id}"
            if result.seed is not None
            else f"evolve: record failed bootstrap {number}"
        )
        _persist_evolve_state(ctx, state_store, label=label)
        ctx.lprint(result.message)
        if result.seed is not None:
            return result.seed
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
