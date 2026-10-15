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
    InvocationFinishedData,
    InvocationStartedData,
    IterationsChangedData,
    PhaseData,
    RoundFinishedData,
    WinnerSelectedData,
    json_value,
)
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.event_journal import EventJournal
from vs_project.api import git_repositories
from vs_runtime.api.core import RuntimeRecord, winner_selection
from vs_runtime.api.infrastructure import (
    AgentExecutionLifecycleEvent,
    AgentExecutionStarted,
    AgentExecutionStatus,
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
    from vs_github.api import GitHubCLI
    from vs_project.api import Git, GitRepositoryFactory, Project, StateStoreFactory
    from vs_runtime.api.core import WinnerSelection
    from vs_runtime.api.infrastructure import (
        EnvironmentSystem,
        ProjectRunResources,
        RunEnvironmentResources,
    )
    from vs_sandbox.api import HostResource
    from vs_sim.api import Clock, Threads


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
    backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    host_resources: tuple[HostResource, ...]
    clock: Clock
    """The run's clock, in seconds since the epoch."""
    threads: Threads
    """Makes the run's threads and locks."""


@dataclass(frozen=True, slots=True)
class _CoreRunControlEvents:
    """Persist runtime transitions in VibeSys's stable core-event format."""

    journal: EventJournal

    def __call__(self, transition: RunControlTransition) -> CoreEvent:
        """Translate one semantic transition without changing its payload."""
        return self.journal.emit(
            CoreEventType(transition.kind.value),
            transition.text,
            agent_kind=transition.agent_kind,
            round_label=transition.round_label,
            execution_id=transition.execution_id,
        )


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


class _RunProjection(Protocol):
    """Product facts needed to diff two committed state projections."""

    @property
    def rounds(self) -> tuple[_RoundProjection, ...]: ...

    @property
    def experiment_revision(self) -> int | None: ...

    @property
    def iterations(self) -> object: ...

    @property
    def projection(self) -> Mapping[str, object] | None: ...


class CommittedStateProjector(Protocol):
    """Project just-committed plugin state into product run facts."""

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


def _baseline_drift_detail(event: RootBaselineDrifted) -> str:
    """Describe which pre-run paths an attempt changed or removed."""
    parts = []
    if event.modified:
        parts.append(f"modified: {', '.join(event.modified)}")
    if event.removed:
        parts.append(f"removed: {', '.join(event.removed)}")
    return "; ".join(parts)


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

    def committed(self, previous: BaseModel | None, current: BaseModel) -> None:
        """Publish the durable state hint before the events derived from it."""
        self._integration.publish_committed_state(self._namespace, current)
        _emit_core_commit_events(
            self._integration.events, self._project(previous), self._project(current)
        )
        _emit_winner_selected(
            self._integration.events,
            winner_selection(previous) if isinstance(previous, RuntimeRecord) else None,
            winner_selection(current) if isinstance(current, RuntimeRecord) else None,
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
        clock: Clock,
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
        self.control = create_run_control_channel(_CoreRunControlEvents(self.events), threads)
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
        del project
        self.events.attach(log_dir, run_id or log_dir.parent.name)

    def close(self) -> None:
        """Release integration-owned resources."""
        if self._closed:
            return
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
            data=AgentExecutionFinishedData(result=result, error=event.error),
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
