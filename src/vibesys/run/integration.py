"""Core-owned ports used by application entrypoints and server adapters."""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any, Protocol

from vibesys.run.event_journal import EventJournal

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pydantic import BaseModel

    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vibesys.sandbox.run_environment import RunEnvironment, RunEnvironmentRequest
    from vs_project import Project
    from vs_sandbox import HostResource, ProjectPathPolicy


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    """Identity and effective prompt returned at an invocation boundary."""

    execution_id: str | None
    user_prompt: str


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """Resolved agent implementation used by an auxiliary run surface."""

    driver: str
    provider: str
    model: str
    role_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """Neutral run resources from which an application may build another agent."""

    config: Config
    compute_backend: ComputeBackend
    model: Any
    skills: tuple[str, ...]
    skill_source_dirs: tuple[Path, ...]
    environment: RunEnvironment
    environment_request: RunEnvironmentRequest
    run_environment_sandboxed: bool
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]


@dataclass(frozen=True, slots=True)
class RunAttachment:
    """Core resources exposed to optional application-owned run surfaces."""

    project: Project
    run_id: str
    workspace: Path
    log_dir: Path
    agent_backend: str
    agent_defaults: AgentSelection
    agent_runtime: AgentRuntimeResources


class InvocationLifecycle(Protocol):
    """Optional application control around core agent invocations."""

    def start(  # noqa: D102, PLR0913
        self,
        kind: str,
        round_label: str,
        user_prompt: str,
        system_prompt: str = "",
        *,
        driver: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        participates_in_run_control: bool = True,
    ) -> ExecutionHandle: ...

    def finish(  # noqa: D102
        self,
        kind: str,
        round_label: str,
        *,
        result: Any = None,  # noqa: ANN401
        error: BaseException | None = None,
        execution_id: str | None = None,
    ) -> None: ...

    def presentation_scope(  # noqa: D102
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> AbstractContextManager[None]: ...


class DirectInvocationLifecycle:
    """Headless invocation policy with no pause or steering behavior."""

    def start(  # noqa: D102, PLR0913
        self,
        kind: str,
        round_label: str,
        user_prompt: str,
        system_prompt: str = "",
        *,
        driver: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        participates_in_run_control: bool = True,
    ) -> ExecutionHandle:
        del kind, round_label, system_prompt, driver, provider, model, participates_in_run_control
        return ExecutionHandle(execution_id=uuid.uuid4().hex, user_prompt=user_prompt)

    def finish(  # noqa: D102
        self,
        kind: str,
        round_label: str,
        *,
        result: Any = None,  # noqa: ANN401
        error: BaseException | None = None,
        execution_id: str | None = None,
    ) -> None:
        del kind, round_label, result, error, execution_id

    @contextmanager
    def presentation_scope(  # noqa: D102
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> Generator[None]:
        del agent_kind, round_label, execution_id
        yield


class RunIntegration(Protocol):
    """Narrow application integration accepted by core run construction."""

    events: EventJournal
    invocations: InvocationLifecycle

    def attach(  # noqa: D102
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None: ...

    def attach_run(  # noqa: D102
        self, attachment: RunAttachment
    ) -> Callable[[], None] | None: ...

    def publish_committed_state(  # noqa: D102
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None: ...

    def close(self) -> None: ...  # noqa: D102


class LocalRunIntegration:
    """Default integration used when a run is driven without a server."""

    def __init__(self) -> None:
        """Compose a durable journal with direct invocation control."""
        self.events = EventJournal()
        self.invocations: InvocationLifecycle = DirectInvocationLifecycle()
        from vibesys.render import output_sink  # noqa: PLC0415

        self._unsubscribe_output = output_sink().subscribe(self.events.record)
        self._closed = False

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

    def attach_run(self, attachment: RunAttachment) -> Callable[[], None] | None:
        """Ignore optional application surfaces in a local run."""
        del attachment
        return None

    def publish_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Ignore transient state hints when no application consumes them."""
        del namespace, state, changed_keys
