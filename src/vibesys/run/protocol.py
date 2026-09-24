"""The run-context surface loops are allowed to depend on.

``LoopContext`` captures what loop implementations actually consume from
``_RunContext`` (verified by grepping ``ctx.`` usage across ``loops/`` and
``main.py``).  Loop entry points type against this protocol instead of the
concrete class, which keeps the facade's construction internals out of the
loops' contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from vibesys.constants import ComputeBackend
    from vibesys.evaluators.input_manifest import WorkspaceSource
    from vibesys.profilers import ProfilerKind
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.git_tracker import GitTracker
    from vibesys.run.state import RunState
    from vs_agent.api import AgentClientProtocol, AgentProgress
    from vs_project.api import Project, StateTransition

T = TypeVar("T", bound=BaseModel)


class LoopContext(Protocol):
    """Runtime services and state consumed by the loop implementations."""

    # -- run identity / configuration -----------------------------------------
    backend: ComputeBackend
    model_name: str
    profiler_kind: ProfilerKind
    ref_name: str
    workspace_sources: tuple[WorkspaceSource, ...]

    # -- collaborators --------------------------------------------------------
    events: EventJournal
    agent_client: AgentClientProtocol
    judge_backend: Any
    run_environment: Any
    run_environment_view: Any
    git: GitTracker
    state: RunState
    project: Project
    run_id: str

    # -- paths ----------------------------------------------------------------
    @property
    def project_root(self) -> Path:
        """Root directory of the project being optimized."""
        ...

    @property
    def log_dir(self) -> Path:
        """Directory for logs belonging to the active run."""
        ...

    @property
    def workspace(self) -> Path:
        """Mutable candidate workspace directory."""
        ...

    @property
    def run_log_path(self) -> Path:
        """Path to the active run's append-only log."""
        ...

    @property
    def skill_source_paths(self) -> list[Path]:
        """Directories searched for skills during the run."""
        ...

    # -- agent-facing commands ------------------------------------------------
    @property
    def objective_location(self) -> str:
        """Configured location of the run objective."""
        ...

    @property
    def judge_accuracy_command(self) -> str | None:
        """Optional command used to judge accuracy."""
        ...

    @property
    def judge_benchmark_command(self) -> str | None:
        """Optional command used to judge benchmark results."""
        ...

    @property
    def profiler_benchmark_command(self) -> str | None:
        """Optional command used to benchmark profiler runs."""
        ...

    # -- services -------------------------------------------------------------
    def lprint(self, text: str) -> None:
        """Write a line to the run log and configured output sink."""
        ...

    def switch_log_file(self, label: int | str) -> None:
        """Switch the active run log to a named phase or round."""
        ...

    def invoke(
        self,
        *,
        kind: str,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str = "",
        progress: AgentProgress | None = None,
        **extra: Any,
    ) -> T:
        """Invoke the configured agent with typed response validation."""
        ...

    def progress(self, progress: AgentProgress) -> AbstractContextManager[None]:
        """Expose the active agent progress scope."""
        ...

    def snapshot_workspace(self, label: str) -> None:
        """Record a named snapshot of the current workspace."""
        ...

    def trusted_input_changes(self) -> list[str]:
        """List changes to trusted inputs since the run baseline."""
        ...

    def begin_completed_round(
        self,
        round_number: int,
        *,
        state_transition: StateTransition,
    ) -> None:
        """Stage a completed round and its matching state transition."""
        ...

    def persist_completed_round(self) -> None:
        """Persist the active completed-round record."""
        ...

    def publish_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Publish state committed by the active round."""
        ...

    def reselect_gpu(self) -> None:
        """Refresh the selected GPU before the next operation."""
        ...

    def wait_for_debug(self, step: str) -> None:
        """Wait at a named debugging checkpoint."""
        ...

    def close(self) -> None:
        """Release resources owned by the run context."""
        ...
