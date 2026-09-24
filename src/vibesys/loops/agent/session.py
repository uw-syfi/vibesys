"""Agent-policy run state and round transactions over one opened run host.

The four orchestrators own the visible scheduling. This module keeps the
existing synchronous role implementations on one worker thread while the
orchestrator awaits planning, attempts, and durable round completion.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

from vibesys import constants
from vibesys.domains.registry import resolve_domain
from vibesys.events import CoreEventType, EventStatus, RoundFinishedData
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.framework import _LocalAgentPolicyIO
from vibesys.loops.agent.hypotheses import (
    adopt_metric_space,
)
from vibesys.loops.agent.hypothesis_controller import HypothesisEngine, publish_experiments_changed
from vibesys.loops.agent.policy_attempts import (
    AttemptDecision,
    AttemptPolicy,
    AttemptRequest,
    AttemptServices,
    AttemptState,
    JudgeSkipped,
    JudgeSkipReason,
    run_official_gates,
)
from vibesys.loops.agent.policy_ports import RoundPreparation, RoundPreparationServices
from vibesys.loops.agent.policy_scheduler import (
    RoundSelection,
    RoundSelectionRequest,
    TerminalRequest,
    apply_requested_rollback,
    select_round,
    transition_round,
)
from vibesys.loops.agent.policy_support import (
    _CarryOver,
    _finalize_agent_run,
    _pareto_archive_summary,
    _terminal_workspace_notice,
)
from vibesys.loops.agent.record import RecordInput, build_round_record
from vibesys.loops.agent.roles import BuiltInAgentRoles
from vibesys.loops.agent.state import AgentRunState, AgentRunStateStore
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from vibesys.loops.agent.orchestration import AgentOrchestrationOptions
    from vibesys.loops.agent.policy_attempts import MultiAttemptPolicy, SingleAttemptPolicy
    from vibesys.loops.agent.policy_profile import ProfilePolicy
    from vibesys.orchestration.runtime import RunContext

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True)
class AgentRound:
    """A selected hypothesis and its mutable bounded-attempt state."""

    selection: RoundSelection
    request: AttemptRequest
    attempt: AttemptState


@dataclass(frozen=True)
class AgentSessionPolicy:
    """Policy decisions supplied by one agent strategy during migration."""

    profile: ProfilePolicy
    preparation_factory: Callable[[RoundPreparationServices], RoundPreparation]
    attempt_factory: Callable[[AttemptServices], AttemptPolicy]
    template_dir: Path
    state_namespace: str


class AgentSession:
    """Policy-owned adapter for one host, one agent state, and one round cursor."""

    def __init__(
        self,
        host: RunContext,
        options: AgentOrchestrationOptions,
        policy: AgentSessionPolicy,
    ) -> None:
        """Bind one opened host and the selected policy components."""
        self.host = host
        self.options = options
        self.profile = policy.profile
        self.preparation_factory = policy.preparation_factory
        self.attempt_factory = policy.attempt_factory
        self.template_dir = policy.template_dir
        self.state_namespace = policy.state_namespace
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vibesys-agent-policy")
        self._progress_cm = None
        self._closed = False

    @classmethod
    async def open(
        cls,
        host: RunContext,
        options: AgentOrchestrationOptions,
        policy: AgentSessionPolicy,
    ) -> AgentSession:
        """Bind existing host resources and load the policy's durable state."""
        session = cls(host, options, policy)
        try:
            await session._call(session._initialize)
        except BaseException:
            session.worker.shutdown(wait=True)
            raise
        return session

    async def _call(self, operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        loop = asyncio.get_running_loop()
        pending = loop.run_in_executor(self.worker, partial(operation, *args, **kwargs))
        cancelled = False
        while not pending.done():
            try:
                await asyncio.wait({pending})
            except asyncio.CancelledError:
                cancelled = True
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
        if cancelled:
            pending.exception()
            raise asyncio.CancelledError
        return pending.result()

    def _initialize(self) -> None:
        request = self.host.request
        bundle = request.input_bundle
        ctx = self.host.run_context
        options = self.options
        domain = resolve_domain(bundle.domain)
        modality = options.modality
        if modality is None and domain.name is constants.DomainName.LLM_SERVING:
            modality = "text_generation"
        objective = request.objective or bundle.objective
        self.ctx = ctx
        self.objective = objective
        self.framework_benchmark_configured = (
            bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None
        )
        agents = BuiltInAgentRoles.bind(ctx)
        output_sink().run_configured(
            run_log_path=str(ctx.run_log_path),
            project_root=str(ctx.project_root),
            objective=objective,
        )
        roadmap_path, progress_path = issue_board.resolve_paths(
            ctx.workspace, options.memory_layout
        )
        issue_board.ensure_progress_file(progress_path)
        issue_board.ensure_roadmap_file(roadmap_path)
        issue_board.write_validation_recipe_schema(progress_path)
        progress_location = issue_board.display_path(progress_path, ctx.workspace)
        roadmap_location = issue_board.display_path(roadmap_path, ctx.workspace)
        pareto_archive_location = issue_board.display_path(
            issue_board.pareto_archive_path(progress_path), ctx.workspace
        )
        self.progress_path = progress_path
        state_store = AgentRunStateStore(ctx.state.portable(self.state_namespace))
        previous_state = state_store.load_optional()
        state = adopt_metric_space(previous_state or AgentRunState(), options.metric_space)
        if previous_state != state:
            state_store.save(state)
            ctx.state.commit("agent: initialize policy state", state_store.namespace)
            ctx.publish_committed_state(self.state_namespace, state)
        self.state_store = state_store
        self.state = state
        self.history = RoundHistory(records=state.rounds)
        self.records = self.history.records
        self.carry = _CarryOver(regression_info=_terminal_workspace_notice(self.records))
        self.round_number = len(self.records) + 1
        self.last_single_response = None
        self.last_profile_focus = "general latency hotspots on /v1/completions"
        self.io = _LocalAgentPolicyIO(
            ctx=ctx,
            agents=agents,
            template_dir=self.template_dir,
            state_namespace=self.state_namespace,
            state_store=state_store,
            domain_definition=domain,
            objective=objective,
            modality=modality,
            interface=options.interface,
            progress_path=progress_path,
            progress_location=progress_location,
            roadmap_path=roadmap_path,
            roadmap_location=roadmap_location,
            pareto_archive_location=pareto_archive_location,
            framework_benchmark_configured=self.framework_benchmark_configured,
            benchmark_result=bundle.benchmark_result,
            benchmark_result_protocol=bundle.benchmark_result_protocol,
            objectives=list(state.metrics.objectives),
            accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
            benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
            judge_every=options.judge_every,
            official_eval_every=options.official_eval_every,
        )
        round_services = RoundPreparationServices(
            turns=self.io, profiler_enabled=ctx.profiler_kind is not ProfilerKind.NONE
        )
        self.preparation = self.preparation_factory(round_services)
        self.attempt_services = AttemptServices(
            turns=self.io,
            effects=self.io,
            max_rounds=options.max_rounds,
            max_retries_per_round=options.max_retries_per_round,
            judge_every=options.judge_every,
            official_eval_every=options.official_eval_every,
        )
        self.attempt_policy = self.attempt_factory(self.attempt_services)
        self.engine = HypothesisEngine.create(state, config=self.profile.config)

    @property
    def has_next_round(self) -> bool:
        """Whether the durable cursor remains inside the policy's total budget."""
        return self.round_number <= self.options.max_rounds

    @asynccontextmanager
    async def round_scope(self) -> AsyncIterator[None]:
        """Close the progress context even when a turn or checkpoint fails."""
        try:
            yield
        finally:
            await self._call(self._end_round)

    async def select_hypothesis(self) -> AgentRound:
        """Plan or continue one hypothesis and mark its round before paid work."""
        return await self._call(self._select_hypothesis)

    def _select_hypothesis(self) -> AgentRound:
        ctx = self.ctx
        round_number = self.round_number
        options = self.options
        ctx.switch_log_file(f"round{round_number:03d}")
        issue_board.write_pareto_archive(
            self.progress_path,
            _pareto_archive_summary(self.records, self.state.metrics),
        )
        round_progress = RoundProgress(round_number, options.max_rounds)
        ctx.lprint(f"\n{'=' * 60}\n  {round_progress.label()}\n{'=' * 60}\n")
        self._progress_cm = ctx.progress(round_progress)
        self._progress_cm.__enter__()
        selection = select_round(
            self.profile,
            self.preparation,
            self.io,
            RoundSelectionRequest(
                engine=self.engine,
                state=self.state,
                records=self.records,
                carry=self.carry,
                round_number=round_number,
                max_rounds=options.max_rounds,
                official_eval_every=options.official_eval_every,
                previous_single_response=self.last_single_response,
            ),
        )
        self.engine = selection.engine
        self.state = apply_requested_rollback(self.io, selection, self.history, self.records)
        hypothesis = selection.hypothesis
        request = AttemptRequest(
            round_number=round_number,
            plan=selection.plan,
            planned_official_reason=selection.planned_official_reason,
            records=self.records,
            active_hypothesis=hypothesis,
            engine=self.engine,
            last_profile_focus=self.last_profile_focus,
        )
        attempt = AttemptState(
            agent_run_state=self.state,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return AgentRound(selection, request, attempt)

    async def remaining_attempts(self, round_: AgentRound) -> range:
        """Return retries after the durable paid-work cursor."""
        return await self._call(self._remaining_attempts, round_)

    def _remaining_attempts(self, round_: AgentRound) -> range:
        first = self.io.next_attempt(round_.request.round_number)
        limit = self.options.max_retries_per_round
        if first > limit:
            raise RuntimeError(  # noqa: TRY003
                f"Round {round_.request.round_number} already persisted {first - 1} "
                f"implementer attempts, exhausting max_retries_per_round={limit}; "
                "refusing to overwrite or replay paid work."
            )
        if first > 1:
            self.io.log(
                f"[resume] round {round_.request.round_number} continues at durable "
                f"attempt {first}/{limit}"
            )
        return range(first, limit + 1)

    async def begin_attempt(self, round_: AgentRound, retry: int) -> None:
        """Initialize the next paid attempt before any agent invocation."""
        await self._call(self._begin_attempt, round_, retry)

    def _begin_attempt(self, round_: AgentRound, retry: int) -> None:
        self.io.log(f"\n--- attempt {retry}/{self.options.max_retries_per_round} ---\n")
        round_.attempt.retry = retry
        round_.attempt.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
        round_.attempt.official_reason = None

    async def implement(self, round_: AgentRound) -> bool:
        """Invoke the multi-agent implementer once."""
        policy = cast("MultiAttemptPolicy", self.attempt_policy)
        return await self._call(policy.implement, round_.request, round_.attempt)

    async def review(self, round_: AgentRound) -> AttemptDecision:
        """Apply sparse review, judge, and local validation."""
        policy = cast("MultiAttemptPolicy", self.attempt_policy)
        return await self._call(policy.review, round_.request, round_.attempt)

    async def combined_turn(self, round_: AgentRound) -> AttemptDecision:
        """Invoke the single combined implementation and review agent."""
        policy = cast("SingleAttemptPolicy", self.attempt_policy)
        return await self._call(policy.run_attempt, round_.request, round_.attempt)

    async def official_gates(self, round_: AgentRound) -> bool:
        """Run trusted gates only when the policy requested them."""
        return await self._call(
            run_official_gates, self.attempt_services, round_.request, round_.attempt
        )

    async def commit_round(self, round_: AgentRound) -> None:
        """Atomically write the completed round and its next policy state."""
        await self._call(self._commit_round, round_)

    def _end_round(self) -> None:
        if self._progress_cm is not None:
            self._progress_cm.__exit__(None, None, None)
            self._progress_cm = None

    async def finish(self) -> bool:
        """Finalize retained work after the total round budget is exhausted."""
        return await self._call(self._finish)

    def _finish(self) -> bool:
        self.ctx.lprint(f"Reached max_rounds={self.options.max_rounds}. Stopping.")
        _finalize_agent_run(
            self.ctx,
            records=self.records,
            space=self.state.metrics,
            progress_path=self.progress_path,
        )
        return True

    async def close(self) -> None:
        """Release the policy worker; the host closes run resources itself."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._call(self._end_round)
        finally:
            self.worker.shutdown(wait=True)

    def _commit_round(self, round_: AgentRound) -> None:
        attempt = round_.attempt
        projection = self.attempt_policy.project_performance(round_.request, attempt)
        if projection.next_single_response is not None:
            self.last_single_response = projection.next_single_response
        record = build_round_record(
            RecordInput(
                state=attempt.agent_run_state,
                records=self.records,
                round_number=self.round_number,
                hypothesis=round_.selection.hypothesis,
                plan=round_.selection.plan,
                attempt=attempt,
                projection=projection,
                reviewed=self.attempt_policy.reviewed(attempt),
                framework_benchmark_configured=self.framework_benchmark_configured,
                accuracy_configured=bool(self.ctx.judge_accuracy_command),
                candidate_commit=self.ctx.git.current_sha(),
                backend_name=self.ctx.agent_client.backend_name,
                driver_name=self.ctx.agent_client.driver_name,
                provider=self.ctx.agent_client.provider,
                model=self.ctx.agent_client.model_for_kind("implementer"),
            )
        )
        terminal = transition_round(
            self.attempt_policy,
            self.profile,
            TerminalRequest(
                engine=self.engine,
                state=attempt.agent_run_state,
                hypothesis=round_.selection.hypothesis,
                attempt=attempt,
                record=record,
                records=self.records,
                carry=self.carry,
                reviewed=self.attempt_policy.reviewed(attempt),
                max_retries_per_round=self.options.max_retries_per_round,
            ),
        )
        transition = self.state_store.transition(terminal.state)
        self.ctx.begin_completed_round(self.round_number, state_transition=transition)
        self.records.append(record)
        if terminal.exhaustion_feedback is not None:
            issue_board.append_exhaustion_note(
                self.progress_path,
                self.round_number,
                self.options.max_retries_per_round,
                terminal.exhaustion_feedback,
            )
        self.ctx.persist_completed_round()
        self.engine = terminal.engine
        self.state = terminal.state
        self.carry = terminal.carry
        self.round_number += 1
        publish_experiments_changed(
            self.ctx,
            self.state,
            "round_persisted",
            (record.hypothesis_id,),
            namespace=self.state_namespace,
        )
        self.ctx.events.emit(
            CoreEventType.ROUND_FINISHED,
            status=(
                EventStatus.COMPLETED
                if attempt.passed or not record.reviewed
                else EventStatus.FAILED
            ),
            round_label=f"round-{record.round_number}",
            data=RoundFinishedData(
                attempts=attempt.retry,
                judge_verdict=(
                    "pass" if attempt.passed else "fail" if record.reviewed else "skipped"
                ),
                perf_metric=projection.metric,
                perf_unit=projection.unit,
                profile_skipped=projection.profile_skipped,
            ),
        )
