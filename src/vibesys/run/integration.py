"""Core-owned run resources handed off to an optional application surface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

from vibesys.run.event_journal import EventJournal
from vibesys.run.run_control import RunControlChannel

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vibesys.sandbox.run_environment import RunEnvironment, RunEnvironmentRequest
    from vs_project import Project
    from vs_sandbox import HostResource, ProjectPathPolicy


@dataclass(frozen=True, slots=True)
class RunResourceHandoff:
    """Core run resources an application may use to build another agent surface.

    Type-erased at the core/application boundary: core builds one from the
    facts it already resolved while assembling a run and hands it to whatever
    listener `LocalRunIntegration.add_resource_listener` registered, with no
    knowledge of what (if anything) the application does with it. The server
    converts this into `server.run_attachment.RunAttachment` for its own
    experiment-chat wiring.
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
    run_environment_sandboxed: bool
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]


class LocalRunIntegration:
    """Default integration used when a run is driven without a server.

    The only implementation of the shape `vibesys.api.session` builds
    internally for every run: an `EventJournal` for the run's event stream,
    a `RunControlChannel` for steer/pause/resume/stop, and two optional
    one-shot listener seams (`add_committed_state_listener`,
    `add_resource_listener`) an application can register before the run
    starts. Neither seam is a Protocol an application implements against;
    both are plain callables `RunSession.on_committed_view`/
    `RunSession.on_run_resources` register on the session's own instance.
    """

    def __init__(self) -> None:
        """Compose a durable journal with direct invocation control."""
        self.events = EventJournal()
        self.control = RunControlChannel(self.events)
        from vibesys.render import output_sink  # noqa: PLC0415

        self._unsubscribe_output = output_sink().subscribe(self.events.record)
        self._closed = False
        self._committed_state_listener: (
            Callable[[str, BaseModel, tuple[str, ...] | None], None] | None
        ) = None
        self._resource_listener: Callable[[RunResourceHandoff], None] | None = None

    def attach(  # noqa: D102
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        del project
        self.events.attach(log_dir, run_id or log_dir.parent.name)

    def close(self) -> None:
        """Release integration-owned resources."""
        if self._closed:
            return
        self._closed = True
        self._unsubscribe_output()

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

    def add_resource_listener(self, listener: Callable[[RunResourceHandoff], None]) -> None:
        """Register the sole application consumer of this run's resource handoff."""
        self._resource_listener = listener

    def publish_resources(self, handoff: RunResourceHandoff) -> None:
        """Hand off this run's resources to the registered listener, if any."""
        if self._resource_listener is not None:
            self._resource_listener(handoff)
