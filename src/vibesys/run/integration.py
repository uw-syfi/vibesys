"""Core-owned run resources handed off to an optional application surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEvent,
    CoreEventType,
    EventStatus,
    InvocationFinishedData,
    InvocationStartedData,
    PhaseData,
    json_value,
)
from vibesys.run.agent_events import CoreAgentEventSink
from vibesys.run.event_journal import EventJournal
from vs_runtime.api.infrastructure import (
    AgentExecutionLifecycleEvent,
    AgentExecutionStarted,
    AgentExecutionStatus,
    RunControlTransition,
    create_run_control_channel,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vs_project.api import Project
    from vs_runtime.api.infrastructure import (
        RunEnvironment,
        RunEnvironmentRequest,
        RunEnvironmentSession,
    )
    from vs_sandbox.api import HostResource


@dataclass(frozen=True, slots=True)
class RunResources:
    """Private resource bundle shared by core composition and ``RunSession``.

    This never crosses ``vibesys.api``.  ``RunSession`` projects it to
    ``RunReady`` and uses it privately when constructing a managed auxiliary
    agent, so frontends cannot observe configuration, sandboxes, path policy,
    or host-resource grants.
    """

    project: Project
    run_id: str
    workspace: Path
    log_dir: Path
    agent_backend: str
    driver: str
    provider: str
    model: str
    role_models: tuple[str, ...]
    config: Config
    compute_backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    environment: RunEnvironment
    environment_request: RunEnvironmentRequest
    environment_session: RunEnvironmentSession
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

    def add_committed_state_listener(
        self, listener: Callable[[str, BaseModel, tuple[str, ...] | None], None]
    ) -> None:
        """Register the sole application projection of freshly committed state."""
        self._committed_state_listener = listener

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
