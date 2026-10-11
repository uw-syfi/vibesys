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
    PhaseData,
    RoundFinishedData,
    WorkstreamAttemptData,
    json_value,
)
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.event_journal import EventJournal
from vs_runtime.api.infrastructure import (
    AgentExecutionLifecycleEvent,
    AgentExecutionStarted,
    AgentExecutionStatus,
    RunControlTransition,
    WorkspaceResourceEvent,
    WorkspaceRestoreFailed,
    create_run_control_channel,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Literal, TextIO

    from pydantic import BaseModel

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vs_agent.api import AgentEventSink
    from vs_project.api import Project
    from vs_runtime.api.infrastructure import (
        ProjectRunResources,
        RunEnvironmentResources,
    )
    from vs_sandbox.api import HostResource


def run_log_emitter(events: AgentEventSink) -> Callable[[str, TextIO], None]:
    """Write run-log text and publish the same diagnostic on its run stream."""

    def emit(text: str, log_file: TextIO) -> None:
        events.agent_output(text + "\n", channel="diagnostic")
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
    driver: str
    provider: str
    model: str
    role_models: tuple[str, ...]
    config: Config
    backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    host_resources: tuple[HostResource, ...]


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
            client_id=transition.client_id,
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


class _WorkstreamProjection(Protocol):
    """Product facts needed to translate one workstream's attempt diagnostics."""

    @property
    def hypothesis_id(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def attempt_limit(self) -> int: ...

    @property
    def refunded(self) -> int: ...

    @property
    def last_error(self) -> str | None: ...

    @property
    def setup_failure(self) -> bool: ...


class _RunProjection(Protocol):
    """Product facts needed to diff two committed state projections."""

    @property
    def rounds(self) -> tuple[_RoundProjection, ...]: ...

    @property
    def workstreams(self) -> tuple[_WorkstreamProjection, ...]: ...

    @property
    def experiment_revision(self) -> int | None: ...


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


class _StateCommitObserver:
    """Publish product projections only after runtime durability succeeds."""

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
        """Publish the durable state hint before its derived semantic events."""
        self._integration.publish_committed_state(self._namespace, current)
        _emit_commit_events(
            self._integration.events,
            self._project(previous),
            self._project(current),
        )

    def _project(self, value: BaseModel | None) -> _RunProjection | None:
        projector = self._projector
        if projector is None or value is None:
            return None
        return projector.project_committed(self._namespace, value, run_id=self._run_id)


def _round_entries(view: _RunProjection | None) -> dict[int, _RoundProjection]:
    if view is None:
        return {}
    return {round_summary.number: round_summary for round_summary in view.rounds}


def _workstream_entries(view: _RunProjection | None) -> dict[str, _WorkstreamProjection]:
    if view is None:
        return {}
    return {item.hypothesis_id: item for item in view.workstreams}


def _workstream_diagnostics_key(
    item: _WorkstreamProjection,
) -> tuple[int, int, str | None, bool]:
    """Return the fields whose change means this workstream's diagnostics moved."""
    return (item.attempt, item.refunded, item.last_error, item.setup_failure)


def _emit_commit_events(
    events: EventJournal,
    before: _RunProjection | None,
    after: _RunProjection | None,
) -> None:
    """Emit semantic changes newly made observable by one durable commit."""
    if after is None:
        return
    before_rounds = _round_entries(before)
    after_rounds = _round_entries(after)
    new_round_numbers = sorted(number for number in after_rounds if number not in before_rounds)
    for number in new_round_numbers:
        _emit_round_finished(events, after_rounds[number])
    before_workstreams = _workstream_entries(before)
    for hypothesis_id, item in _workstream_entries(after).items():
        previous = before_workstreams.get(hypothesis_id)
        if previous is not None and _workstream_diagnostics_key(
            previous
        ) == _workstream_diagnostics_key(item):
            continue
        _emit_workstream_attempt(events, item, previous)
    if before is None:
        return
    before_revision = before.experiment_revision
    after_revision = after.experiment_revision
    if after_revision is not None and after_revision != before_revision:
        reason = "round_persisted" if new_round_numbers else "active_hypothesis_changed"
        events.emit(
            CoreEventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason=reason, revision=after_revision),
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


def _emit_workstream_attempt(
    events: EventJournal,
    item: _WorkstreamProjection,
    previous: _WorkstreamProjection | None,
) -> None:
    """Emit one workstream's current attempt diagnostics.

    ``repeated_error`` is true only when the previous summary was itself a
    setup failure with identical error text: the deterministic case where
    retrying spends budget on the same outcome every time (see
    ``Workstreams._record_failure`` in ``orchestration/dynamic/workstream.py``,
    whose own ``repeated`` computation this mirrors from durable state
    instead of threading the transient local through the event).
    """
    repeated = (
        previous is not None
        and previous.setup_failure
        and item.setup_failure
        and previous.last_error is not None
        and previous.last_error == item.last_error
    )
    events.emit(
        CoreEventType.WORKSTREAM_ATTEMPT_UPDATED,
        data=WorkstreamAttemptData(
            hypothesis_id=item.hypothesis_id,
            attempt=item.attempt,
            attempt_limit=item.attempt_limit,
            refunded=item.refunded,
            last_error=item.last_error,
            setup_failure=item.setup_failure,
            repeated_error=repeated,
        ),
    )


class LocalRunIntegration:
    """Default integration used when a run is driven without a server.

    The only implementation of the shape `vibesys.api.session` builds
    internally for every run: an `EventJournal` for the run's event stream,
    a `RunControlChannel` for steer/pause/resume/stop, one application-facing
    committed-state listener, and one private resource listener owned by
    `RunSession`.
    """

    def __init__(self) -> None:
        """Compose a durable journal with direct invocation control."""
        self.events = EventJournal()
        self.agent_events = CoreAgentEventSink(self.events.record)
        self.control = create_run_control_channel(_CoreRunControlEvents(self.events))
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
                    driver=event.driver,
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
        raise AssertionError(event)

    def add_committed_state_listener(
        self, listener: Callable[[str, BaseModel, tuple[str, ...] | None], None]
    ) -> None:
        """Register the sole application projection of freshly committed state."""
        self._committed_state_listener = listener

    def state_commit_observer(
        self,
        run_id: str,
        projector: CommittedStateProjector | None,
        namespace: str,
    ) -> _StateCommitObserver:
        """Bind durable plugin state to product hints and semantic events."""
        return _StateCommitObserver(self, run_id, projector, namespace)

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
