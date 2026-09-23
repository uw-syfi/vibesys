"""Orchestrator-driven build loop.

An Orchestrator agent decides each round what the Implementer should build and
what pass criteria the Judge should enforce, optionally asking a Profiler to
collect kernel-level data first.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Literal

from vibesys import constants
from vibesys.agent_spec_config import resolve_agent_driver
from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_run_context
from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.input_manifest import (  # noqa: TC001  # tracked: #288
    BenchmarkResult,
    ProfileGuidedInput,
    WorkspaceSource,
)
from vibesys.events import (
    CoreEventType,
    EventStatus,
    FrameworkSource,
    RoundFinishedData,
)
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.attempt import (
    JudgeSkipped,
    JudgeSkipReason,
    recorded_judge_verdict,
)
from vibesys.loops.agent.hypotheses import (
    ResolutionEvidence,
    adopt_metric_space,
    metric_baseline,
    record_metric_value,
    resolve_hypothesis_outcome,
    scalar_candidate_retained,
    trusted_perf_provenance,
    update_active_hypothesis,
)
from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    persist_active_hypothesis,
    persist_agent_run_state,
    plan_changed_keys,
    publish_experiments_changed,
)
from vibesys.loops.agent.model import (
    AgentRunState,
)
from vibesys.loops.agent.orchestration import (
    AgentOrchestrationOptions,
    compare_resume_descriptors,
    descriptor_from_options,
    legacy_configuration_from_options,
    recorded_objectives,
)
from vibesys.loops.agent.policy_attempts import (
    AttemptDecision,
    AttemptRequest,
    AttemptServices,
    AttemptState,
    run_official_gates,
)
from vibesys.loops.agent.policy_flow import control_flow_for, validate_control_flow
from vibesys.loops.agent.policy_profile import (
    ProfileOutcomeInput,
    ProfilePreparation,
)
from vibesys.loops.agent.policy_rounds import (
    RoundPreparationRequest,
    RoundPreparationServices,
)
from vibesys.loops.agent.policy_support import (
    _FAILED_HYPOTHESIS_OUTCOMES,
    _INTERFACES,
    _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW,
    DEFAULT_INTERFACE,
    _backfill_revert_commit,
    _CarryOver,
    _detect_plateau,
    _finalize_agent_run,
    _implementation_keeps_hypothesis_active,
    _implementation_requests_continuation,
    _official_evaluation_reason,
    _pareto_archive_dominators,
    _pareto_archive_summary,
    _provisional_candidate_retained,
    _provisional_candidates_since_official,
    _run_orchestrator_plan,
    _terminal_workspace_notice,
)
from vibesys.loops.agent.roles import (
    BuiltInAgentRoles,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.gates import (
    BenchmarkContract,
)
from vibesys.loops.metrics import (
    Measurement,
    MetricSpace,
)
from vibesys.profilers import (
    ProfilerKind,
)
from vibesys.render.sink import output_sink
from vibesys.run import LocalRunIntegration, RepositoryVisibility, RunStateNamespace
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    ProfilerSummary,
    SingleAgentRoundResponse,
)
from vs_agent.api import (
    AgentBackend,
    RoundProgress,
)
from vs_loop_state.api import RoundHistory, RoundRecord

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_loop(  # noqa: C901, PLR0912, PLR0913, PLR0915  # tracked: #288
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
    metrics: MetricSpace,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    evaluator_path: Path | None = None,
    evaluator_package_root: Path | None = None,
    benchmark_result: BenchmarkResult | None = None,
    benchmark_result_protocol: Literal[2] | None = None,
    accuracy_timeout_seconds: int | None = None,
    benchmark_timeout_seconds: int | None = None,
    max_rounds: int = 24,
    max_retries_per_round: int = 3,
    judge_every: int = 3,
    official_eval_every: int = 3,
    memory_layout: str = "files",
    start_round: int | None = 1,
    existing: bool = False,
    operator_constraints: tuple[str, ...] = (),
    trusted_input_baseline: str | None = None,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str | None = None,
    inner_loop: str = "multi-agent",
    domain: constants.DomainName | None = None,
    interface: str = DEFAULT_INTERFACE,
    remote_repo: str | None = None,
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE,
    integration: LocalRunIntegration | None = None,
    outer_loop: Literal["agent", "profile-guided"] = "agent",
    profile_guided: ProfileGuidedInput | None = None,
) -> bool:
    """Run the orchestrator-driven build loop.

    Returns True iff the orchestrator declared the objective met within
    ``max_rounds``.  Returns False when the round budget is exhausted.

    ``inner_loop`` selects how each round's implement/judge/profile work
    is dispatched:

    - ``"multi-agent"`` (default): three specialist agents — implementer,
      judge, profiler — invoked in sequence.
    - ``"single-agent"``: one agent does all three in a single
      invocation per retry. Pre-round decision and standalone profiler
      passes are skipped; the prior round's profile output is fed to the
      orchestrator as ``profiler_summary``.

    ``interface`` selects only the evaluator-to-candidate process boundary:

    - ``"inprocess"`` (default): evaluator-owned code invokes the candidate
      directly using the input-defined contract.
    - ``"service"``: evaluator-owned code communicates with a running service
      through its network interface.

    Language, tooling, and artifact requirements come from the domain and input
    bundle rather than the process-boundary mode.
    """
    validate_control_flow(inner_loop, outer_loop, profile_guided)
    if max_retries_per_round < 1:
        # Guard against a zero-iteration retry loop: with no attempts the
        # round bookkeeping below would reference an unbound loop variable.
        raise ValueError(f"max_retries_per_round must be >= 1, got {max_retries_per_round}")  # noqa: TRY003  # tracked: #288
    if judge_every < 1:
        raise ValueError(f"judge_every must be >= 1, got {judge_every}")  # noqa: TRY003  # tracked: #288
    if official_eval_every < 1:
        raise ValueError(f"official_eval_every must be >= 1, got {official_eval_every}")  # noqa: TRY003  # tracked: #288
    if memory_layout not in issue_board.MEMORY_LAYOUTS:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Unknown memory_layout {memory_layout!r}; "
            f"choose from {', '.join(issue_board.MEMORY_LAYOUTS)}"
        )
    if interface not in _INTERFACES:
        raise ValueError(f"Unknown interface {interface!r}; choose from {', '.join(_INTERFACES)}")  # noqa: TRY003  # tracked: #288
    if domain is None:
        raise ValueError("domain is required; declare [agent].domain in vibesys.input.toml")  # noqa: TRY003  # tracked: #288
    # Resolve the registered domain once (fail fast on an unknown name). The
    # per-role files carry language, tooling, and use-case-specific contracts.
    domain_definition = resolve_domain(domain)
    objectives = list(metrics.objectives)
    # Either result contract means a framework-owned benchmark runs this round,
    # which is what the prompts and the official-evaluation record key off.
    framework_benchmark_configured = benchmark_result is not None or (
        benchmark_result_protocol is not None
    )
    if modality is None and domain_definition.name is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = run_environment or make_run_environment_spec()
    normalized_config = as_config(config)
    resolved_agent_backend = (
        "stub"
        if agent_backend == "stub"
        else str(agent_backend or normalized_config.agent.backend or AgentBackend.CLI)
    )
    options = AgentOrchestrationOptions(
        inner_loop=inner_loop,
        interface=interface,
        model=normalized_config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=resolve_agent_driver(normalized_config).value
        if resolved_agent_backend == "cli"
        else None,
        cli_provider=(cli_provider or normalized_config.agent.cli_provider or "codex")
        if agent_backend != "stub"
        else None,
        compute_backend=backend.value,
        profiler=profiler_kind.value,
        max_rounds=max_rounds,
        max_retries_per_round=max_retries_per_round,
        judge_every=judge_every,
        official_eval_every=official_eval_every,
        memory_layout=memory_layout,
        modality=modality,
        cli_timeout=normalized_config.agent.cli_timeout,
        default_reasoning_effort=normalized_config.thinking.level,
        outer_model=normalized_config.agent.outer.model,
        outer_reasoning_effort=normalized_config.agent.outer.reasoning_effort,
        inner_model=normalized_config.agent.inner.model,
        inner_reasoning_effort=normalized_config.agent.inner.reasoning_effort,
        operator_constraints=operator_constraints,
        objectives=recorded_objectives(metrics, benchmark_result),
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
        benchmark_output_argument=BenchmarkContract(
            result_spec=benchmark_result,
            result_protocol=benchmark_result_protocol,
        ).output_argument,
        objective=objective,
        existing=existing,
        legacy_configuration_factory=lambda resolved_profiler: legacy_configuration_from_options(
            options,
            outer_loop=outer_loop,
            run_environment=run_environment_record(run_environment),
            profiler=resolved_profiler.value,
        ),
        orchestration_descriptor=lambda resolved_profiler: descriptor_from_options(
            options, outer_loop=outer_loop, profiler=resolved_profiler.value
        ),
        orchestration_resume=compare_resume_descriptors,
        trusted_input_baseline=trusted_input_baseline,
        debug=debug,
        profiler_kind=profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=remote_repo,
        repo_visibility=repo_visibility,
        agent_state_model_type=AgentRunState,
        integration=integration,
    )
    # This policy declares its participants once. The handles share the
    # context's existing client; turn order and handoffs remain below.
    agents = BuiltInAgentRoles.bind(ctx)
    output_sink().run_configured(
        run_log_path=str(ctx.run_log_path),
        project_root=str(ctx.project_root),
        objective=objective,
    )
    roadmap_path, progress_path = issue_board.resolve_paths(ctx.workspace, memory_layout)
    issue_board.ensure_progress_file(progress_path)
    issue_board.ensure_roadmap_file(roadmap_path)
    issue_board.write_validation_recipe_schema(progress_path)
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    roadmap_location = issue_board.display_path(roadmap_path, ctx.workspace)
    pareto_archive_path = issue_board.pareto_archive_path(progress_path)
    pareto_archive_location = issue_board.display_path(pareto_archive_path, ctx.workspace)
    portable_agent_state = ctx.state.portable(RunStateNamespace.AGENT)
    local_agent_state = ctx.state.local(RunStateNamespace.AGENT)
    state_store = AgentRunStateStore(portable_agent_state)
    legacy_records = ctx.state.completed_rounds()
    agent_run_state = state_store.migrate_legacy(
        rounds=legacy_records,
        local_namespace=local_agent_state,
        legacy_space=metrics,
    )
    # The one write of the run's metric space. Everything downstream -- round
    # projection, retention, the Pareto frontier, ``--resume`` reprojection,
    # and the server read path -- takes it from the persisted state, so the
    # launching task file wins here and nowhere else re-reads it. Rounds that
    # already recorded a comparison keep it; only their derivation changes.
    agent_run_state = adopt_metric_space(agent_run_state, metrics)
    active_hypothesis = agent_run_state.active_hypothesis
    if active_hypothesis is not None and _backfill_revert_commit(
        active_hypothesis, agent_run_state.rounds
    ):
        agent_run_state = update_active_hypothesis(agent_run_state, active_hypothesis)
    # Replace the legacy portable namespace exactly, then remove the local
    # restart checkpoint only after its unified replacement is committed.
    state_store.save(agent_run_state)
    state_store.cleanup_legacy_portable([record.round_number for record in legacy_records])
    ctx.state.commit("agent: migrate unified hypothesis state", state_store.namespace)
    ctx.publish_committed_state("agent", agent_run_state)
    state_store.cleanup_legacy_local(local_agent_state)
    round_history = RoundHistory(records=agent_run_state.rounds)
    records = round_history.records
    carry = _CarryOver(regression_info=_terminal_workspace_notice(records))
    round_number = start_round if start_round is not None else len(records) + 1
    if round_number > max_rounds:
        try:
            if existing and records:
                ctx.lprint(
                    f"This run already completed {len(records)} rounds; "
                    "finalizing its retained result."
                )
                _finalize_agent_run(
                    ctx,
                    records=records,
                    space=agent_run_state.metrics,
                    progress_path=progress_path,
                )
                return True
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"This run has completed {round_number - 1} rounds; max_rounds={max_rounds} "
                "is a total limit. Increase --max-rounds to continue."
            )
        finally:
            ctx.close()

    # A policy may carry the previous combined response into the next plan.
    last_single_agent_response: SingleAgentRoundResponse | None = None
    last_profile_focus: str = "general latency hotspots on /v1/completions"
    round_services = RoundPreparationServices(
        ctx=ctx,
        agents=agents,
        objective=objective,
        modality=modality,
        interface=interface,
        domain_definition=domain_definition,
        progress_path=progress_path,
        progress_location=progress_location,
    )
    attempt_services = AttemptServices(
        ctx=ctx,
        agents=agents,
        state_store=state_store,
        domain_definition=domain_definition,
        objective=objective,
        modality=modality,
        interface=interface,
        progress_path=progress_path,
        progress_location=progress_location,
        pareto_archive_location=pareto_archive_location,
        framework_benchmark_configured=framework_benchmark_configured,
        benchmark_result=benchmark_result,
        benchmark_result_protocol=benchmark_result_protocol,
        objectives=objectives,
        accuracy_timeout_seconds=accuracy_timeout_seconds,
        benchmark_timeout_seconds=benchmark_timeout_seconds,
        max_rounds=max_rounds,
        max_retries_per_round=max_retries_per_round,
        judge_every=judge_every,
        official_eval_every=official_eval_every,
    )
    flow = control_flow_for(
        inner_loop, outer_loop, profile_guided, round_services, attempt_services
    )
    engine = HypothesisEngine.create(
        agent_run_state,
        config=flow.profile_config,
    )
    try:
        while round_number <= max_rounds:
            ctx.switch_log_file(f"round{round_number:03d}")
            issue_board.write_pareto_archive(
                progress_path,
                _pareto_archive_summary(records, agent_run_state.metrics),
            )
            round_progress = RoundProgress(round_number, max_rounds)
            ctx.lprint(f"\n{'=' * 60}\n  {round_progress.label()}\n{'=' * 60}\n")
            with ctx.progress(round_progress):
                # The designer runs only when selecting a new causal claim.
                # A continuing hypothesis remains owned by its persistent
                # implementer session without another designer intervention.
                profiler_summary: ProfilerSummary | None = None
                if active_hypothesis is None:
                    engine, agent_run_state = flow.prepare_profile(
                        ProfilePreparation(
                            ctx=ctx,
                            engine=engine,
                            state_store=state_store,
                            state=agent_run_state,
                            round_number=round_number,
                        )
                    )
                    profiler_summary = flow.profiler_summary(
                        RoundPreparationRequest(
                            round_number=round_number,
                            records=records,
                            carry=carry,
                            previous_single_response=last_single_agent_response,
                        )
                    )
                    plateau_warning = _detect_plateau(records)
                    provisional_candidates = _provisional_candidates_since_official(records)
                    plan = _run_orchestrator_plan(
                        ctx,
                        agent=agents.orchestrator,
                        agent_run_state=agent_run_state,
                        round_number=round_number,
                        objective=objective,
                        profiler_summary=profiler_summary,
                        carry=carry,
                        progress_path=progress_path,
                        progress_location=progress_location,
                        roadmap_location=roadmap_location,
                        pareto_archive_location=pareto_archive_location,
                        plateau_warning=plateau_warning,
                        modality=modality,
                        interface=interface,
                        domain_definition=domain_definition,
                        framework_benchmark_enabled=framework_benchmark_configured,
                        official_eval_every=official_eval_every,
                        provisional_candidates=provisional_candidates,
                        official_eval_cadence_due=(
                            provisional_candidates + 1 >= official_eval_every
                        ),
                        profile_guidance=engine.controller.guidance,
                    )
                    parent_round = (
                        plan.revert_to_round
                        if plan.revert_to_round is not None
                        else round_number - 1
                        if round_number > 1
                        else None
                    )
                    parent_record = next(
                        (
                            record
                            for record in reversed(records)
                            if record.round_number == parent_round
                        ),
                        None,
                    )
                    engine = engine.replace_state(agent_run_state).start(
                        plan,
                        started_round=round_number,
                        parent_round=parent_round,
                        parent_commit=(
                            parent_record.commit
                            if parent_record is not None and parent_record.commit is not None
                            else ctx.git.current_sha()
                        ),
                    )
                    agent_run_state = engine.state
                    active_hypothesis = agent_run_state.active_hypothesis
                    assert active_hypothesis is not None  # noqa: S101  # started above
                    plan = active_hypothesis.plan
                    persist_agent_run_state(
                        ctx,
                        state_store,
                        agent_run_state,
                        label=f"agent: start hypothesis {plan.hypothesis_id}",
                    )
                    publish_experiments_changed(
                        ctx, agent_run_state, "active_hypothesis_changed", plan_changed_keys(plan)
                    )
                else:
                    plan = active_hypothesis.plan
                    issue_board.append_hypothesis_continuation(
                        progress_path,
                        round_number,
                        plan=plan,
                        started_round=active_hypothesis.started_round,
                        continuation_step=active_hypothesis.next_step or plan.task,
                    )
                    ctx.lprint(
                        f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
                    )
                planned_official_reason = _official_evaluation_reason(
                    records=records,
                    round_number=round_number,
                    max_rounds=max_rounds,
                    official_eval_every=official_eval_every,
                    requested=plan.request_official_evaluation,
                    candidate_ready=True,
                )
                planned_official_reason = flow.official_reason(planned_official_reason, engine)
                # No early stop. Previously OrchestratorPlan had a ``done`` field that
                # could halt the loop; it was removed because the orchestrator
                # can't reliably tell when the objective is "fully met" and
                # early-stopping masks further optimization opportunities.
                # --- Optional rollback ---
                if plan.revert_to_round is not None and not active_hypothesis.revert_applied:
                    target = next(
                        (r for r in records if r.round_number == plan.revert_to_round),
                        None,
                    )
                    if target and target.commit:
                        rollback_commit, failed_child_round = round_history.resolve_rollback_commit(
                            target, _FAILED_HYPOTHESIS_OUTCOMES
                        )
                        assert rollback_commit is not None  # noqa: S101  # tracked: #288
                        # Restore the tree without moving HEAD so subsequent
                        # commits land on the current branch as new commits
                        # after the reverted state.
                        memory_paths = tuple(
                            str(path.relative_to(ctx.workspace))
                            for path in (roadmap_path, progress_path, pareto_archive_path)
                        )
                        if ctx.git.checkout_tree(
                            rollback_commit,
                            clean=True,
                            preserve_paths=memory_paths,
                        ):
                            if failed_child_round is None:
                                ctx.lprint(
                                    "Reverted workspace to round "
                                    f"{plan.revert_to_round} ({rollback_commit[:8]})."
                                )
                            else:
                                ctx.lprint(
                                    "Reverted workspace to the pre-hypothesis parent of "
                                    f"failed round {failed_child_round} ({rollback_commit[:8]}), "
                                    f"based on parent round {plan.revert_to_round}."
                                )
                            active_hypothesis.revert_applied = True
                            active_hypothesis.revert_commit = rollback_commit
                            active_hypothesis.parent_commit = rollback_commit
                            agent_run_state = persist_active_hypothesis(
                                ctx,
                                state_store,
                                agent_run_state,
                                active_hypothesis,
                                label=(f"agent: set hypothesis {plan.hypothesis_id} parent"),
                            )
                        else:
                            output_sink().framework_warning(
                                "rollback was not applied; will retry round "
                                f"{plan.revert_to_round} on the next continuation",
                                source=FrameworkSource.LOOP,
                            )
                    else:
                        output_sink().framework_warning(
                            f"cannot revert: no commit recorded for round {plan.revert_to_round}",
                            source=FrameworkSource.LOOP,
                        )
                # The issue board marks paid attempts before another agent turn.
                attempt_request = AttemptRequest(
                    round_number=round_number,
                    plan=plan,
                    planned_official_reason=planned_official_reason,
                    records=records,
                    active_hypothesis=active_hypothesis,
                    engine=engine,
                    last_profile_focus=last_profile_focus,
                )
                attempt_state = AttemptState(
                    agent_run_state=agent_run_state,
                    feedback=active_hypothesis.feedback,
                    revalidation_required=active_hypothesis.gate_revalidation_pending,
                )
                first_retry = issue_board.next_implementer_attempt(progress_path, round_number)
                if first_retry > max_retries_per_round:
                    raise RuntimeError(  # noqa: TRY003  # tracked: #288
                        f"Round {round_number} already persisted "
                        f"{first_retry - 1} implementer attempts, exhausting "
                        f"max_retries_per_round={max_retries_per_round}; refusing "
                        "to overwrite or replay paid work."
                    )
                if first_retry > 1:
                    ctx.lprint(
                        f"[resume] round {round_number} continues at durable "
                        f"attempt {first_retry}/{max_retries_per_round}"
                    )
                for retry in range(first_retry, max_retries_per_round + 1):
                    ctx.lprint(f"\n--- attempt {retry}/{max_retries_per_round} ---\n")
                    attempt_state.retry = retry
                    attempt_state.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
                    attempt_state.official_reason = None
                    decision = flow.run_attempt(attempt_request, attempt_state)
                    if decision is AttemptDecision.FINISH:
                        break
                    if decision is AttemptDecision.OFFICIAL and run_official_gates(
                        attempt_services, attempt_request, attempt_state
                    ):
                        break
                agent_run_state = attempt_state.agent_run_state
                feedback = attempt_state.feedback
                passed = attempt_state.passed
                implementation = attempt_state.implementation
                single_agent_response = attempt_state.single_agent_response
                framework_benchmark = attempt_state.framework_benchmark
                completed_official_evaluation_reason = (
                    attempt_state.official_reason if passed else None
                )
                attempt_judge = attempt_state.judge
                accepted_metrics: dict[str, float] = {}
                accepted_evaluation_artifact: str | None = None
                # --- Record round result & update carry-over ---
                # Only the final attempt describes the round, so its outcome is
                # the one the record and the lifecycle transition read.
                commit = ctx.git.current_sha()
                # `profile_skipped` is True when no fresh profile ran this round
                # (cold-start or the orchestrator/framework decided to skip).
                # The plateau detector ignores skipped-profile rounds so cached
                # / inherited perf numbers don't masquerade as fresh measurements.
                #
                # The selected flow projects fresh evidence for this round.
                projection = flow.project_performance(attempt_request, attempt_state)
                perf_metric = projection.metric
                perf_unit = projection.unit
                perf_provenance = projection.provenance
                profile_skipped = projection.profile_skipped
                accepted_metrics = projection.accepted_metrics
                accepted_evaluation_artifact = projection.accepted_evaluation_artifact
                if projection.next_single_response is not None:
                    last_single_agent_response = projection.next_single_response
                # A prior retry may have been reviewed before the implementer
                # returned a different terminal result.  Record and transition
                # from the final attempt, not from any earlier audit in the
                # round; otherwise an unreviewed ``disproven`` retry is
                # mislabeled rejected and the dead hypothesis stays active.
                reviewed = flow.reviewed(attempt_state)
                if implementation is not None:
                    candidate_disposition = implementation.candidate_disposition.value
                    candidate_metrics = dict(implementation.candidate_metrics)
                    candidate_evaluation_artifact = implementation.candidate_evaluation_artifact
                    candidate_operating_point = implementation.candidate_operating_point
                    candidate_retention_reason = implementation.candidate_retention_reason
                elif single_agent_response is not None:
                    candidate_disposition = single_agent_response.candidate_disposition.value
                    candidate_metrics = dict(single_agent_response.candidate_metrics)
                    candidate_evaluation_artifact = (
                        single_agent_response.candidate_evaluation_artifact
                    )
                    candidate_operating_point = single_agent_response.candidate_operating_point
                    candidate_retention_reason = single_agent_response.candidate_retention_reason
                else:
                    candidate_disposition = CandidateDisposition.UNASSESSED.value
                    candidate_metrics = {}
                    candidate_evaluation_artifact = None
                    candidate_operating_point = ""
                    candidate_retention_reason = ""
                # A framework-gate retry may correctly return no fresh candidate
                # row. Preserve the judge-approved provisional evidence for the
                # unchanged checkpoint just as canonical evidence is preserved.
                if (
                    candidate_disposition == CandidateDisposition.UNASSESSED.value
                    and active_hypothesis.gate_revalidation_pending
                    and active_hypothesis.gate_approved_candidate_disposition
                    == CandidateDisposition.PARETO_FRONTIER.value
                ):
                    candidate_disposition = active_hypothesis.gate_approved_candidate_disposition
                    candidate_metrics = dict(active_hypothesis.gate_approved_candidate_metrics)
                    candidate_evaluation_artifact = (
                        active_hypothesis.gate_approved_candidate_evaluation_artifact
                    )
                    candidate_operating_point = (
                        active_hypothesis.gate_approved_candidate_operating_point
                    )
                    candidate_retention_reason = (
                        active_hypothesis.gate_approved_candidate_retention_reason
                    )
                # A result-protocol benchmark measures the complete objective
                # row, so it replaces the provisional metrics outright.
                if perf_metric is not None and framework_benchmark.row is not None:
                    accepted_metrics = dict(framework_benchmark.row)
                # A legacy [benchmark.result] benchmark reports a single trusted
                # scalar via perf_metric/perf_unit but leaves accepted_metrics
                # empty (only implementer-reported evals populate it). Without a
                # comparable objective row, _record_candidate_metrics falls back
                # to the provisional candidate_metrics for frontier dominance.
                # Promote the trusted scalar into the objective row so official
                # framework measurements drive the frontier. Does not apply when
                # a protocol row is present: the row above is already complete.
                if not accepted_metrics and perf_metric is not None and perf_unit is not None:
                    accepted_metrics = {perf_unit: perf_metric}
                official_evaluation = (
                    passed
                    and completed_official_evaluation_reason is not None
                    and ctx.agent_client.backend_name != "stub"
                    and (bool(ctx.judge_accuracy_command) or framework_benchmark_configured)
                )
                declared_outcome = (
                    implementation.hypothesis_outcome
                    if implementation is not None
                    else HypothesisOutcome.NOMINATED
                    if single_agent_response is not None
                    else None
                )
                primary_objective = objectives[0] if objectives else None
                metric_name = (
                    framework_benchmark.metric_name
                    or (primary_objective.name if primary_objective is not None else None)
                    or perf_unit
                )
                metric_direction = framework_benchmark.metric_direction or (
                    primary_objective.direction if primary_objective is not None else None
                )
                official_metric = (
                    accepted_metrics.get(metric_name) if metric_name is not None else perf_metric
                )
                if official_metric is None and not accepted_metrics:
                    official_metric = perf_metric
                parent_record = metric_baseline(
                    parent_round=active_hypothesis.parent_round,
                    parent_commit=active_hypothesis.parent_commit,
                    metric=metric_name,
                    rounds=records,
                )
                baseline_metric = (
                    record_metric_value(parent_record, metric_name)
                    if parent_record is not None
                    else None
                )
                # A headline metric is framework-owned unless the implementer
                # self-reported it. This is the trust boundary the rest of the
                # round applies: resolution, scalar and Pareto retention, the
                # recorded delta, and trusted Pareto-parent selection all read
                # it, so an untrusted number never drives a dominance decision.
                framework_provenance = trusted_perf_provenance(perf_provenance)
                # The round's headline reading is ordered against its causal
                # baseline exactly once, here, and stored on the record. Every
                # later reader -- resume reprojection and the server -- consumes
                # the stored answer instead of re-deriving it.
                #
                # An implementer-reported number is never ordered at all: the
                # comparison stays None, which is what makes the hypothesis
                # resolve UNMEASURED rather than borrowing a verdict from a
                # number the framework did not measure.
                space = agent_run_state.metrics
                official_reading = (
                    Measurement(
                        metric=metric_name,
                        value=official_metric,
                        direction=metric_direction,
                    )
                    if metric_name is not None and official_metric is not None
                    else None
                )
                perf_comparison = (
                    space.compare(
                        official_reading,
                        Measurement(
                            metric=metric_name,
                            value=baseline_metric,
                            direction=metric_direction,
                        )
                        if metric_name is not None and baseline_metric is not None
                        else None,
                    )
                    if official_evaluation and official_metric is not None and framework_provenance
                    else None
                )
                hypothesis_resolution = resolve_hypothesis_outcome(
                    ResolutionEvidence(
                        declared=declared_outcome,
                        passed=passed,
                        reviewed=reviewed,
                        comparison=perf_comparison,
                    )
                )
                disposition = CandidateDisposition(candidate_disposition)
                if not reviewed:
                    candidate_retained = _provisional_candidate_retained(disposition)
                elif not passed:
                    candidate_retained = False
                elif (
                    official_evaluation and framework_provenance and objectives and accepted_metrics
                ):
                    candidate_retained = not _pareto_archive_dominators(
                        accepted_metrics,
                        records,
                        space,
                    )
                elif official_evaluation and framework_provenance:
                    prior_readings = [
                        Measurement(
                            metric=metric_name,
                            value=value,
                            direction=metric_direction,
                        )
                        for record in records
                        if metric_name is not None
                        and record.official_evaluation
                        and trusted_perf_provenance(record.perf_provenance)
                        and (value := record_metric_value(record, metric_name)) is not None
                    ]
                    candidate_retained = scalar_candidate_retained(
                        space.compare_to_best(official_reading, prior_readings)
                    )
                else:
                    # No trusted framework measurement (or an implementer
                    # self-report): retain provisionally on the implementer's
                    # disposition, never on the untrusted metric.
                    candidate_retained = _provisional_candidate_retained(disposition)
                perf_delta_pct = None
                if (
                    framework_provenance
                    and official_metric is not None
                    and baseline_metric is not None
                    and baseline_metric != 0
                ):
                    perf_delta_pct = (
                        (official_metric - baseline_metric) / abs(baseline_metric) * 100
                    )
                completed_record = RoundRecord(
                    round_number=round_number,
                    commit=commit,
                    perf_metric=perf_metric,
                    perf_unit=perf_unit,
                    passed=passed,
                    profile_skipped=profile_skipped,
                    hypothesis_id=plan.hypothesis_id,
                    hypothesis_declared_outcome=(
                        declared_outcome.value if declared_outcome is not None else None
                    ),
                    # ``RoundRecord.reviewed`` derives from this verdict, so
                    # the record cannot claim a verdict it never received.
                    judge_verdict=recorded_judge_verdict(attempt_judge),
                    hypothesis_outcome=(
                        hypothesis_resolution.value
                        if hypothesis_resolution is not None
                        else declared_outcome.value
                        if declared_outcome is not None
                        else None
                    ),
                    hypothesis_claim=plan.hypothesis or None,
                    hypothesis_task=plan.task or None,
                    hypothesis_parent_round=active_hypothesis.parent_round,
                    hypothesis_parent_commit=active_hypothesis.parent_commit,
                    metrics=accepted_metrics,
                    evaluation_artifact=accepted_evaluation_artifact,
                    official_evaluation=official_evaluation,
                    official_evaluation_reason=(
                        completed_official_evaluation_reason
                        if (
                            ctx.agent_client.backend_name != "stub"
                            and (bool(ctx.judge_accuracy_command) or framework_benchmark_configured)
                        )
                        else None
                    ),
                    candidate_disposition=candidate_disposition,
                    candidate_metrics=candidate_metrics,
                    candidate_evaluation_artifact=candidate_evaluation_artifact,
                    candidate_operating_point=candidate_operating_point,
                    candidate_retention_reason=candidate_retention_reason,
                    candidate_retained=candidate_retained,
                    perf_direction=metric_direction,
                    perf_baseline_round=(
                        parent_record.round_number if parent_record is not None else None
                    ),
                    perf_baseline_commit=(
                        parent_record.commit if parent_record is not None else None
                    ),
                    perf_baseline_metric=baseline_metric,
                    perf_delta_pct=perf_delta_pct,
                    perf_comparison=perf_comparison,
                    perf_provenance=perf_provenance,
                    implementer_driver=ctx.agent_client.driver_name,
                    implementer_provider=ctx.agent_client.provider,
                    implementer_model=ctx.agent_client.model_for_kind("implementer"),
                )
                # Compute the completed lifecycle transition in memory so its
                # exact representation can enter the write-ahead journal before
                # progress notes or durable state are mutated.
                next_active_hypothesis = active_hypothesis.clone()
                if flow.keeps_hypothesis_active(
                    attempt_state, next_active_hypothesis.continuation_rounds
                ):
                    next_active_hypothesis.feedback = feedback if reviewed and not passed else None
                    assert implementation is not None  # noqa: S101  # tracked: #288
                    next_active_hypothesis.next_step = implementation.next_step
                    next_active_hypothesis.continuation_rounds += 1
                elif passed:
                    next_active_hypothesis = None
                elif (
                    reviewed
                    and next_active_hypothesis.continuation_rounds
                    < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
                ):
                    # A rejected review may justify another scoped repair, but
                    # it consumes the same bounded ownership lease as an
                    # implementer-declared continuation. Otherwise repeated
                    # judge failures can bypass the designer indefinitely.
                    next_active_hypothesis.feedback = feedback
                    next_active_hypothesis.next_step = (
                        implementation.next_step
                        if implementation is not None
                        and _implementation_requests_continuation(implementation)
                        else None
                    )
                    next_active_hypothesis.continuation_rounds += 1
                elif reviewed or (
                    implementation is not None
                    and not _implementation_keeps_hypothesis_active(
                        implementation,
                        continuation_rounds=next_active_hypothesis.continuation_rounds,
                    )
                ):
                    next_active_hypothesis = None
                else:
                    next_active_hypothesis.feedback = None
                    next_active_hypothesis.next_step = (
                        implementation.next_step if implementation is not None else None
                    )
                profile_outcome = flow.profile_outcome(
                    ProfileOutcomeInput(round_number, passed, official_evaluation, perf_delta_pct)
                )
                engine = engine.replace_state(agent_run_state).complete_round(
                    completed_record,
                    next_active=next_active_hypothesis,
                    profile_outcome=profile_outcome,
                )
                next_agent_run_state = engine.state
                state_transition = state_store.transition(next_agent_run_state)
                ctx.begin_completed_round(
                    round_number,
                    state_transition=state_transition,
                )
                records.append(completed_record)

                if not passed and records[-1].reviewed:
                    issue_board.append_exhaustion_note(
                        progress_path,
                        round_number,
                        max_retries_per_round,
                        feedback or "",
                    )
                    carry.exhaustion_info = (
                        f"Round {round_number} did not pass after "
                        f"{max_retries_per_round} attempts. Last judge feedback: "
                        f"{feedback or '(empty)'}"
                    )
                    carry.regression_info = None
                elif passed:
                    carry.exhaustion_info = None
                    if flow.terminal_success_needs_parent_choice(
                        attempt_state, active_hypothesis.continuation_rounds
                    ):
                        # A reviewed terminal classification is accepted, but
                        # its implementation edits are still in the workspace.
                        # Give the next designer the same explicit parent-state
                        # decision as an unreviewed terminal result.
                        carry.regression_info = _terminal_workspace_notice(records)
                    elif official_evaluation and candidate_retained is False:
                        carry.regression_info = (
                            f"Round {round_number}'s official candidate was not retained: "
                            f"{perf_metric}{(' ' + perf_unit) if perf_unit else ''}. "
                            "Use its recorded parent and objective directions when choosing "
                            "the next checkpoint."
                        )
                    else:
                        carry.regression_info = None
                else:
                    # A provisional round is normal hypothesis work, not a
                    # judge-loop exhaustion or a performance regression.
                    carry.exhaustion_info = None
                    carry.regression_info = (
                        None
                        if _implementation_keeps_hypothesis_active(
                            implementation,
                            continuation_rounds=active_hypothesis.continuation_rounds,
                        )
                        else _terminal_workspace_notice(records)
                    )

                # The framework, rather than the designer, owns this lifecycle.
                # A continuing implementation keeps its plan and session. An
                # unreviewed terminal result hands control back to the designer;
                # a rejected review keeps the same claim plus reviewer feedback
                # so the implementer can address it on the next round.
                ctx.persist_completed_round()
                agent_run_state = next_agent_run_state
                active_hypothesis = agent_run_state.active_hypothesis
                publish_experiments_changed(
                    ctx, agent_run_state, "round_persisted", (completed_record.hypothesis_id,)
                )
                ctx.events.emit(
                    CoreEventType.ROUND_FINISHED,
                    status=(
                        EventStatus.COMPLETED
                        if passed or not records[-1].reviewed
                        else EventStatus.FAILED
                    ),
                    round_label=f"round-{round_number}",
                    data=RoundFinishedData(
                        attempts=retry,
                        judge_verdict=(
                            "pass" if passed else "fail" if records[-1].reviewed else "skipped"
                        ),
                        perf_metric=perf_metric,
                        perf_unit=perf_unit,
                        profile_skipped=profile_skipped,
                    ),
                )

                round_number += 1

        ctx.lprint(f"Reached max_rounds={max_rounds}. Stopping.")
        _finalize_agent_run(
            ctx,
            records=records,
            space=agent_run_state.metrics,
            progress_path=progress_path,
        )
        return True
    finally:
        ctx.close()
