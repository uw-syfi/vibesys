"""Core-owned run resources handed off to an optional application surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEvent,
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    FrameworkSource,
    FrameworkWarningData,
    InputMeasurementFailedData,
    InputMeasurementFailureKind,
    InvocationFinishedData,
    InvocationStartedData,
    IterationsChangedData,
    MeasurementComparedData,
    PhaseData,
    RoundFinishedData,
    SpanEndedData,
    SpanStartedData,
    SpanUpdatedData,
    SteerDeliveryData,
    WinnerSelectedData,
    WorkstreamFailureData,
    WorkstreamFailureKind,
    WorkstreamFinishedData,
    json_value,
)
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.environment_failure import RunEnvironmentFailure
from vibesys.run.event_journal import EventJournal
from vs_core.api import CheckoutStatus, FlowDriver, FlowState, RunTrace, SpanChange, span_changes
from vs_project.api import git_repositories
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime.api.core import RuntimeRecord, new_comparisons, new_runaways, winner_selection
from vs_runtime.api.infrastructure import (
    AgentExecutionLifecycleEvent,
    AgentExecutionStarted,
    AgentExecutionStatus,
    ChannelRunControl,
    RootBaselineDrifted,
    RunControlTransition,
    WorkspaceResourceEvent,
    WorkspaceRestoreFailed,
    create_run_control_channel,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import Literal, TextIO

    from pydantic import BaseModel

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vs_agent.api import AgentEventSink
    from vs_core.api import Comparison, Runaway
    from vs_github.api import GitHubCLI
    from vs_project.api import Git, GitRepositoryFactory, Project, StateStoreFactory
    from vs_runtime.api.core import WinnerSelection
    from vs_runtime.api.infrastructure import (
        EnvironmentSystem,
        ProjectRunResources,
        RunEnvironmentRequest,
        RunEnvironmentResources,
        RunEnvironmentSession,
    )
    from vs_sandbox.api import HostResource
    from vs_sim.api import Clock, SleepingClock, Threads


def run_log_emitter(events: AgentEventSink) -> Callable[[str, TextIO], None]:
    """Write run-log text and publish the same diagnostic on its run stream."""

    def emit(text: str, log_file: TextIO) -> None:
        events.agent_output(text + "\n", channel="diagnostic")
        if log_file.closed:
            # A conversation that outlives its run, such as an auxiliary scope
            # starting its container, still reports on the event stream.
            return
        log_file.write(text + "\n")
        log_file.flush()

    return emit


@dataclass(frozen=True, slots=True)
class RunResources:
    """Private resource bundle shared by core composition and ``RunSession``.

    Application wiring consumes this only through ``vibesys.api.wiring``.
    ``RunSession`` projects it to ``RunReady`` for frontends, which cannot
    observe configuration, sandboxes, path policy, or host-resource grants.
    """

    project_resources: ProjectRunResources
    environment_resources: RunEnvironmentResources
    agent_backend: str
    provider: str
    model: str
    role_models: tuple[str, ...]
    config: Config
    allow_unvetted_model: bool
    backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    host_resources: tuple[HostResource, ...]
    clock: Clock
    """The run's clock, in seconds since the epoch."""
    threads: Threads
    """Makes the run's threads and locks."""

    def auxiliary(self) -> AuxiliaryResources:
        """What an auxiliary conversation of this run needs of it: no lease and no live session."""
        environment = self.environment_resources
        return AuxiliaryResources(
            project=self.project_resources.project,
            run_id=self.project_resources.state.run_id,
            environment_request=environment.request,
            open_environment_session=environment.open_session,
            agent_backend=self.agent_backend,
            provider=self.provider,
            model=self.model,
            role_models=self.role_models,
            config=self.config,
            allow_unvetted_model=self.allow_unvetted_model,
            backend=self.backend,
            skill_source_dirs=self.skill_source_dirs,
            host_resources=self.host_resources,
            clock=self.clock,
            threads=self.threads,
        )


@dataclass(frozen=True, slots=True)
class AuxiliaryResources:
    """What an auxiliary conversation (chat) reads of a run.

    That is its project, the recipe of its environment, and the agent settings.

    A live run derives it from its ``RunResources``. A reader of a finished run builds it
    directly, because none of the rest of the run's resources (the write lease, the project's
    git tracker, the root environment session, the repository) exist for it, and a conversation
    opens its own environment session from ``open_environment_session``.
    """

    project: Project
    run_id: str
    environment_request: RunEnvironmentRequest
    open_environment_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession]
    agent_backend: str
    provider: str
    model: str
    role_models: tuple[str, ...]
    config: Config
    allow_unvetted_model: bool
    backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    host_resources: tuple[HostResource, ...]
    clock: Clock
    threads: Threads


@dataclass(frozen=True, slots=True)
class CoreRunControlEvents:
    """Persist runtime transitions in VibeSys's stable core-event format."""

    journal: EventJournal

    def __call__(self, transition: RunControlTransition) -> CoreEvent:
        """Translate one semantic transition without changing its payload."""
        return self.journal.emit(
            CoreEventType(transition.kind.value),
            transition.summary,
            agent_kind=transition.agent_kind,
            round_label=transition.round_label,
            execution_id=transition.execution_id,
            data=None
            if transition.delivery is None
            else SteerDeliveryData(delivery=transition.delivery),
        )


class _RoundFailureProjection(Protocol):
    """Product shape of one round's typed failure, independent of its policy's own failure type.

    Structural, not imported from a policy module: ``vibesys.run`` sits below
    every policy in the dependency graph (see architecture.md), so it names
    the shape it needs rather than importing ``vibesys.run.contracts``'s
    concrete ``RoundFailureSummary`` (a module one layer above it).
    """

    @property
    def kind(self) -> str: ...

    @property
    def role(self) -> str | None: ...

    @property
    def turns(self) -> int | None: ...

    @property
    def limit(self) -> int | None: ...

    @property
    def text(self) -> str: ...

    @property
    def operator_text(self) -> str | None: ...


class _InputFailureProjection(Protocol):
    """Product shape of why the input failed its measurement, as ``InputFailureSummary`` has it.

    Structural for the same reason as `_RoundFailureProjection`.
    """

    @property
    def kind(self) -> str: ...

    @property
    def stage(self) -> str | None: ...

    @property
    def output(self) -> str: ...

    @property
    def attempts(self) -> int: ...

    @property
    def compared(self) -> bool: ...


class _RoundProjection(Protocol):
    """Product facts needed to translate one projected round into events."""

    @property
    def number(self) -> int: ...

    @property
    def status(self) -> Literal["completed", "failed"]: ...

    @property
    def attempts(self) -> int: ...

    @property
    def judge_verdict(self) -> Literal["pass", "fail", "skipped"] | None: ...

    @property
    def perf_metric(self) -> float | None: ...

    @property
    def perf_unit(self) -> str | None: ...

    @property
    def profile_skipped(self) -> bool: ...

    @property
    def last_error(self) -> _RoundFailureProjection | None: ...

    @property
    def repeated_error(self) -> bool: ...


class _RunProjection(Protocol):
    """Product facts needed to diff two committed state projections."""

    @property
    def rounds(self) -> tuple[_RoundProjection, ...]: ...

    @property
    def experiment_revision(self) -> int | None: ...

    @property
    def iterations(self) -> object: ...

    @property
    def projection_failure(self) -> str | None:
        """Why the policy's projection of the committed state failed validation; None if not."""
        ...

    @property
    def projection(self) -> Mapping[str, object] | None: ...

    @property
    def input_failure(self) -> _InputFailureProjection | None:
        """Why the input failed its trusted measurement; None while pending or measured."""
        ...


class CommittedStateProjector(Protocol):
    """Project just-committed plugin state into product run facts.

    A projection is derived from the committed state and never raises for a state it cannot
    show; it returns a view whose ``projection_failure`` says why instead.
    """

    def project_committed(
        self,
        namespace: str,
        state: BaseModel,
        *,
        run_id: str,
    ) -> _RunProjection | None:
        """Return the product projection for this namespace and state."""
        ...


def _round_entries(view: _RunProjection | None) -> dict[int, _RoundProjection]:
    if view is None:
        return {}
    return {round_summary.number: round_summary for round_summary in view.rounds}


def _experiments_view(view: _RunProjection) -> Mapping[str, object]:
    """The experiments a frontend shows, without the revision stamp that every commit bumps."""
    return {
        key: value for key, value in (view.projection or {}).items() if key != "experiment_revision"
    }


def _emit_core_commit_events(
    events: EventJournal,
    before: _RunProjection | None,
    after: _RunProjection | None,
) -> None:
    """Emit what one confirmed core commit made newly observable.

    A core run stamps every projection with its commit revision, so the stamp cannot say
    whether the experiments changed. This compares what the projection shows instead.
    """
    if after is None:
        return
    before_rounds = _round_entries(before)
    after_rounds = _round_entries(after)
    new_round_numbers = sorted(number for number in after_rounds if number not in before_rounds)
    for number in new_round_numbers:
        _emit_round_finished(events, after_rounds[number])
        _emit_workstream_finished(events, after_rounds[number])
    _emit_iterations_changed(events, before, after)
    if before is None or _experiments_view(before) == _experiments_view(after):
        return
    events.emit(
        CoreEventType.EXPERIMENTS_CHANGED,
        data=ExperimentsChangedData(
            reason="round_persisted" if new_round_numbers else "active_hypothesis_changed",
            revision=after.experiment_revision,
        ),
    )


INPUT_FAILURE_TAIL_CHARS = 1000
"""How much of the failed stage's output the operator's event carries, after redaction."""


def _emit_input_measurement_failed(events: EventJournal, after: _InputFailureProjection) -> None:
    """Emit ``INPUT_MEASUREMENT_FAILED`` for the failure ``after`` describes."""
    events.emit(
        CoreEventType.INPUT_MEASUREMENT_FAILED,
        status=EventStatus.FAILED,
        data=InputMeasurementFailedData(
            failure=InputMeasurementFailureKind(after.kind),
            stage=after.stage,
            summary=redact_diagnostic_tail(after.output, max_chars=INPUT_FAILURE_TAIL_CHARS),
            attempts=after.attempts,
            compared=after.compared,
        ),
    )


def _emit_iterations_changed(
    events: EventJournal,
    before: _RunProjection | None,
    after: _RunProjection,
) -> None:
    """Publish exactly when a durable iteration projection becomes different."""
    if before is not None and before.iterations == after.iterations:
        return
    revision = getattr(after.iterations, "revision", None)
    events.emit(
        CoreEventType.ITERATIONS_CHANGED,
        data=IterationsChangedData(revision=revision),
    )


def _emit_round_finished(events: EventJournal, round_summary: _RoundProjection) -> None:
    status = EventStatus.FAILED if round_summary.status == "failed" else EventStatus.COMPLETED
    events.emit(
        CoreEventType.ROUND_FINISHED,
        status=status,
        round_label=f"round-{round_summary.number}",
        data=RoundFinishedData(
            attempts=round_summary.attempts,
            judge_verdict=round_summary.judge_verdict or "skipped",
            perf_metric=round_summary.perf_metric,
            perf_unit=round_summary.perf_unit,
            profile_skipped=round_summary.profile_skipped,
        ),
    )


def _workstream_failure_data(
    failure: _RoundFailureProjection | None,
) -> WorkstreamFailureData | None:
    """The round's typed failure, as the wire's frozen ``WorkstreamFailureKind`` vocabulary."""
    if failure is None:
        return None
    operator_text = failure.operator_text
    if operator_text is None:
        operator_text = (
            failure.kind
            if failure.kind == WorkstreamFailureKind.NO_TRUSTED_EVIDENCE.value
            else failure.text
        )
    return WorkstreamFailureData(
        kind=WorkstreamFailureKind(failure.kind),
        role=failure.role,
        turns=failure.turns,
        limit=failure.limit,
        text=operator_text,
    )


def _emit_workstream_finished(events: EventJournal, round_summary: _RoundProjection) -> None:
    """Publish ``WORKSTREAM_FINISHED`` alongside ``ROUND_FINISHED`` for the same round."""
    status = EventStatus.FAILED if round_summary.status == "failed" else EventStatus.COMPLETED
    events.emit(
        CoreEventType.WORKSTREAM_FINISHED,
        status=status,
        round_label=f"round-{round_summary.number}",
        data=WorkstreamFinishedData(
            repeated_error=round_summary.repeated_error,
            last_error=_workstream_failure_data(round_summary.last_error),
        ),
    )


def _emit_winner_selected(
    events: EventJournal, before: WinnerSelection | None, after: WinnerSelection | None
) -> None:
    """Emit ``WINNER_SELECTED`` once per selection core newly records."""
    if after is None or after == before:
        return
    events.emit(
        CoreEventType.WINNER_SELECTED,
        status=EventStatus.COMPLETED,
        data=WinnerSelectedData(
            selection=after.selection, revision=after.revision, settlement_id=after.settlement_id
        ),
    )


def _emit_comparisons(events: EventJournal, comparisons: tuple[Comparison, ...]) -> None:
    """Emit ``MEASUREMENT_COMPARED`` for each comparison core newly logged."""
    for comparison in comparisons:
        events.emit(
            CoreEventType.MEASUREMENT_COMPARED,
            status=EventStatus.COMPLETED,
            data=MeasurementComparedData(
                metric=comparison.metric,
                unit=comparison.unit,
                direction=comparison.direction,
                candidate_commit=comparison.candidate.git_commit
                or comparison.candidate.revision_id.root,
                base_commit=comparison.base.git_commit or comparison.base.revision_id.root,
                candidate_value=comparison.candidate_value,
                base_value=comparison.base_value,
                ratio=comparison.ratio,
                noise=comparison.noise,
                verdict=comparison.verdict,
                candidate_samples=comparison.candidate_samples,
                base_samples=comparison.base_samples,
            ),
        )


def _emit_runaways(events: EventJournal, runaways: tuple[Runaway, ...]) -> None:
    """Warn of each attempt whose agent core newly recorded as asking for too many evaluations."""
    for runaway in runaways:
        events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary="an agent has asked for an unusual number of evaluations",
                detail=(
                    f"the attempt {runaway.scope.owner.root} (generation {runaway.scope.generation})"
                    f" has asked for {runaway.evaluations} evaluations (warning at "
                    f"{runaway.limit}); evaluations are not limited, so nothing was refused"
                ),
                source=FrameworkSource.LOOP,
                source_label="evaluations",
            ),
        )


_SPAN_EVENTS: dict[
    SpanChange, tuple[CoreEventType, type[SpanStartedData | SpanUpdatedData | SpanEndedData]]
] = {
    SpanChange.STARTED: (CoreEventType.SPAN_STARTED, SpanStartedData),
    SpanChange.UPDATED: (CoreEventType.SPAN_UPDATED, SpanUpdatedData),
    SpanChange.ENDED: (CoreEventType.SPAN_ENDED, SpanEndedData),
}


def _run_trace(record: BaseModel | None) -> RunTrace | None:
    """The run trace a committed record's flow state holds (None for any other strategy)."""
    if not isinstance(record, RuntimeRecord):
        return None
    state = record.envelope.strategy
    return FlowDriver.trace(state) if isinstance(state, FlowState) else None


def _emit_span_changes(
    events: EventJournal, before: RunTrace | None, after: RunTrace | None
) -> None:
    """Emit one span event per span the commit started, changed or ended, in trace order."""
    if after is None:
        return
    for change, span in span_changes(before, after):
        event_type, data = _SPAN_EVENTS[change]
        events.emit(event_type, data=data(span=span))


def _baseline_drift_detail(event: RootBaselineDrifted) -> str:
    """Describe which pre-run paths an attempt changed or removed."""
    parts = []
    if event.modified:
        parts.append(f"modified: {', '.join(event.modified)}")
    if event.removed:
        parts.append(f"removed: {', '.join(event.removed)}")
    return "; ".join(parts)


def _emit_adoption_status(
    events: AgentEventSink,
    previous: BaseModel | None,
    current: RuntimeRecord,
) -> None:
    """Project a confirmed publication to the diagnostic stream shared by app and CLI."""
    adoption = current.envelope.core.settlement.adoption
    before = (
        previous.envelope.core.settlement.adoption if isinstance(previous, RuntimeRecord) else None
    )
    if (
        adoption is None
        or not adoption.verified
        or adoption.publication is None
        or adoption == before
    ):
        return
    publication = adoption.publication
    text = f"[result] published {publication.commit[:12]} on {publication.branch}"
    if adoption.checkout is not None:
        status = adoption.checkout.status
        checkout = (
            "updated"
            if status is CheckoutStatus.FAST_FORWARDED
            else f"skipped: {status.value.replace('_', ' ')}"
        )
        text += f"; checkout {checkout}"
    events.agent_output(text + "\n", channel="diagnostic")


class _CoreCommitObserver:
    """Publish a confirmed core commit: the committed-view hint, then its semantic events."""

    def __init__(
        self,
        integration: LocalRunIntegration,
        run_id: str,
        projector: CommittedStateProjector | None,
        namespace: str,
    ) -> None:
        self._integration = integration
        self._run_id = run_id
        self._projector = projector
        self._namespace = namespace
        self._shown: _RunProjection | None = None
        """The last view that was built, which the next commit's events are diffed against."""
        self._seeded = False
        self._failing = False
        self._input_announced: bool | None = None
        """Whether the run's input failure was announced; None until this process first asks."""

    def committed(self, previous: BaseModel | None, current: BaseModel) -> None:
        """Publish the durable state hint before the events derived from it.

        Events are the difference between this commit's view and the last view that was built,
        so the rounds of a stretch whose view could not be built are announced by the first
        commit after it whose view can. A view that cannot be built is reported once, when the
        failure first appears; the run goes on.
        """
        self._integration.publish_committed_state(self._namespace, current)
        if not self._seeded:
            # The first commit this process sees: what it held before was already announced.
            before = self._project(previous)
            if before is not None and before.projection_failure is None:
                self._shown, self._seeded = before, True
        after = self._project(current)
        if after is not None and after.projection_failure is not None:
            self._report_failed_projection(current, after.projection_failure)
        elif after is not None:
            _emit_core_commit_events(self._integration.events, self._shown, after)
            self._announce_input_failure(after)
            self._shown, self._seeded, self._failing = after, True, False
        _emit_winner_selected(
            self._integration.events,
            winner_selection(previous) if isinstance(previous, RuntimeRecord) else None,
            winner_selection(current) if isinstance(current, RuntimeRecord) else None,
        )
        if isinstance(current, RuntimeRecord):
            _emit_adoption_status(self._integration.agent_events, previous, current)
            _emit_comparisons(
                self._integration.events,
                new_comparisons(previous if isinstance(previous, RuntimeRecord) else None, current),
            )
            _emit_runaways(
                self._integration.events,
                new_runaways(previous if isinstance(previous, RuntimeRecord) else None, current),
            )
        _emit_span_changes(self._integration.events, _run_trace(previous), _run_trace(current))

    def _announce_input_failure(self, after: _RunProjection) -> None:
        """Announce the input's failed measurement once per run, judged by the run's journal.

        The failure is a fact of the committed record, so it is announced on the first commit
        that holds it. Whether it was announced is a fact of the run's durable event journal,
        not of the record: a restarted process that loads a record holding the failure does
        not announce it again, and one whose record an earlier version wrote, before the
        announcement existed, announces it now.
        """
        failed = after.input_failure
        if failed is None:
            return
        if self._input_announced is None:
            self._input_announced = self._integration.events.has_recorded(
                CoreEventType.INPUT_MEASUREMENT_FAILED
            )
        if not self._input_announced:
            _emit_input_measurement_failed(self._integration.events, failed)
            self._input_announced = True

    def _report_failed_projection(self, current: BaseModel, failure: str) -> None:
        if self._failing:
            return
        self._failing = True
        revision = current.envelope.revision if isinstance(current, RuntimeRecord) else None
        self._integration.events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=(
                    "the run's view could not be built from its committed state; the run "
                    "goes on, and the rounds it finishes meanwhile are announced once a "
                    "later commit's view can be built"
                ),
                detail=(
                    f"the {self._namespace} projection failed validation at core revision "
                    f"{revision}: {failure}"
                ),
                source=FrameworkSource.LOOP,
                source_label="projection",
            ),
        )

    def _project(self, value: BaseModel | None) -> _RunProjection | None:
        projector = self._projector
        if projector is None or value is None:
            return None
        return projector.project_committed(self._namespace, value, run_id=self._run_id)


class LocalRunIntegration:
    """Default integration used when a run is driven without a server.

    The only implementation of the shape `vibesys.api.session` builds
    internally for every run: an `EventJournal` for the run's event stream,
    a `RunControlChannel` for steer/pause/resume/stop, one application-facing
    committed-state listener, and one private resource listener owned by
    `RunSession`.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-261402 [PLR0913]; each argument is an implementation the wiring code substitutes independently.
        # > Bundling them would only rename the keywords every caller passes; building Git and
        # > the GitHub CLI here from one process launcher would hand this class a primitive
        # > instead of the two roles it passes on.
        self,
        git_repository: GitRepositoryFactory | None = None,
        state_stores: StateStoreFactory | None = None,
        *,
        clock: SleepingClock,
        git: Git,
        github: GitHubCLI,
        threads: Threads,
        system: EnvironmentSystem,
    ) -> None:
        """Compose a durable journal with direct invocation control.

        ``git`` starts the run's Git commands and ``github`` its GitHub CLI commands. ``git_repository`` builds the run's
        ``GitRepository`` implementations; ``None`` opens the configured one over ``git``. ``system`` is the host's threads, sockets
        and processes the run's environments and Slurm evaluations run on.
        ``state_stores`` opens each run's ``StateStore``; ``None`` is the local
        crash-atomic one. ``clock`` counts seconds since the epoch; it stamps the
        run's events, names its logs, and times its boot. ``threads`` makes the run's
        threads and locks.
        """
        self.git = git
        self.github = github
        self.git_repository = git_repositories(git) if git_repository is None else git_repository
        self.system = system
        self.state_stores = state_stores
        self.clock = clock
        self.threads = threads
        self.events = EventJournal(clock, threads)
        self.agent_events = CoreAgentEventSink(self.events.record, clock)
        self.control = create_run_control_channel(CoreRunControlEvents(self.events), threads)
        self.run_control = ChannelRunControl(self.control, threads)
        """The run's one control entry point: every ingress submits here, so ids are shared."""
        self.environment_failure = RunEnvironmentFailure(self.control)
        """Set when the run environment can run none of the run's work; it ends the run."""
        self._closed = False
        self._committed_state_listener: (
            Callable[[str, BaseModel, tuple[str, ...] | None], None] | None
        ) = None
        self._resource_listener: Callable[[RunResources], None] | None = None

    def attach(
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        """Attach event persistence to the run's log directory."""
        identity = run_id or log_dir.parent.name
        if project is not None:
            self.events.enable_runlog(project, identity)
        self.events.attach(log_dir, identity)

    def close(self) -> None:
        """Release integration-owned resources."""
        if self._closed:
            return
        self.events.close()
        self._closed = True

    def agent_execution_event(self, event: AgentExecutionLifecycleEvent) -> CoreEvent:
        """Persist one runtime lifecycle fact in the stable core-event format."""
        fields = {
            "agent_kind": event.agent_id,
            "round_label": event.label,
            "execution_id": event.execution_id,
        }
        if isinstance(event, AgentExecutionStarted):
            self.events.emit(
                CoreEventType.AGENT_EXECUTION_STARTED,
                status=EventStatus.ACTIVE,
                data=AgentExecutionStartedData(
                    stage=event.agent_id,
                    attempt=None,
                    system_prompt=event.system_prompt,
                    user_prompt=event.user_prompt,
                    activity=AgentExecutionActivityData(
                        mode="thinking", summary=f"{event.agent_id} is working"
                    ),
                    provider=event.provider,
                    model=event.model,
                    reasoning_effort=event.reasoning_effort,
                    client=event.client,
                ),
                **fields,
            )
            self.events.emit(
                CoreEventType.PHASE_STARTED,
                status=EventStatus.ACTIVE,
                data=PhaseData(phase=event.agent_id, attempt=None),
                **fields,
            )
            return self.events.emit(
                CoreEventType.INVOCATION_STARTED,
                status=EventStatus.ACTIVE,
                data=InvocationStartedData(
                    system_prompt=event.system_prompt,
                    user_prompt=event.user_prompt,
                ),
                **fields,
            )

        status = EventStatus(AgentExecutionStatus(event.status).value)
        result = json_value(event.result)
        self.events.emit(
            CoreEventType.AGENT_EXECUTION_FINISHED,
            status=status,
            data=AgentExecutionFinishedData(result=result, error=event.error, reply=event.reply),
            **fields,
        )
        self.events.emit(
            CoreEventType.INVOCATION_FINISHED,
            status=status,
            data=InvocationFinishedData(result=result, error=event.error),
            **fields,
        )
        return self.events.emit(
            CoreEventType.PHASE_FINISHED,
            status=status,
            data=PhaseData(phase=event.agent_id, attempt=None),
            **fields,
        )

    def workspace_resource_event(self, event: WorkspaceResourceEvent) -> CoreEvent:
        """Translate a runtime workspace observation into the core event format."""
        if isinstance(event, WorkspaceRestoreFailed):
            return self.events.emit(
                CoreEventType.FRAMEWORK_WARNING,
                data=FrameworkWarningData(
                    summary=(
                        f"could not restore workspace to revision {event.revision[:8]}; "
                        "will retry on a later round"
                    ),
                    source=FrameworkSource.GIT_TRACKING,
                    source_label="rollback",
                ),
            )
        if isinstance(event, RootBaselineDrifted):
            return self.events.emit(
                CoreEventType.FRAMEWORK_WARNING,
                data=FrameworkWarningData(
                    summary="an attempt changed files that existed in the project before the run",
                    detail=_baseline_drift_detail(event),
                    source=FrameworkSource.GIT_TRACKING,
                    source_label="exclusive-root baseline",
                ),
            )
        raise AssertionError(event)

    def add_committed_state_listener(
        self, listener: Callable[[str, BaseModel, tuple[str, ...] | None], None]
    ) -> None:
        """Register the sole application projection of freshly committed state."""
        self._committed_state_listener = listener

    def core_commit_observer(
        self,
        run_id: str,
        projector: CommittedStateProjector | None,
        namespace: str,
    ) -> _CoreCommitObserver:
        """Bind a core run's confirmed commits to product hints and semantic events."""
        return _CoreCommitObserver(self, run_id, projector, namespace)

    def publish_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Forward a transient state hint to the registered listener, if any."""
        if self._committed_state_listener is not None:
            self._committed_state_listener(namespace, state, changed_keys)

    def add_resource_listener(self, listener: Callable[[RunResources], None]) -> None:
        """Register the core session that owns this run's resources."""
        self._resource_listener = listener

    def publish_resources(self, handoff: RunResources) -> None:
        """Give the owning core session access to this run's private resources."""
        if self._resource_listener is not None:
            self._resource_listener(handoff)
