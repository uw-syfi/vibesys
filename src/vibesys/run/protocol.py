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

    from vibesys.agents.contracts import AgentClientProtocol
    from vibesys.agents.progress import AgentProgress
    from vibesys.constants import ComputeBackend
    from vibesys.input_manifest import WorkspaceSource
    from vibesys.profilers import ProfilerKind
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.git_tracker import GitTracker
    from vibesys.run.state import RunState
    from vs_project import Project, StateTransition

T = TypeVar("T", bound=BaseModel)


class LoopContext(Protocol):  # noqa: D101  # tracked: #288
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
    def project_root(self) -> Path: ...  # noqa: D102  # tracked: #288

    @property
    def log_dir(self) -> Path: ...  # noqa: D102  # tracked: #288

    @property
    def workspace(self) -> Path: ...  # noqa: D102  # tracked: #288

    @property
    def run_log_path(self) -> Path: ...  # noqa: D102  # tracked: #288

    @property
    def skill_source_paths(self) -> list[Path]: ...  # noqa: D102  # tracked: #288

    # -- agent-facing commands ------------------------------------------------
    @property
    def objective_location(self) -> str: ...  # noqa: D102  # tracked: #288

    @property
    def judge_accuracy_command(self) -> str | None: ...  # noqa: D102  # tracked: #288

    @property
    def judge_benchmark_command(self) -> str | None: ...  # noqa: D102  # tracked: #288

    @property
    def profiler_benchmark_command(self) -> str | None: ...  # noqa: D102  # tracked: #288

    # -- services -------------------------------------------------------------
    def lprint(self, text: str) -> None: ...  # noqa: D102  # tracked: #288

    def switch_log_file(self, label: int | str) -> None: ...  # noqa: D102  # tracked: #288

    def invoke(  # noqa: D102, PLR0913  # tracked: #288
        self,
        *,
        kind: str,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str = "",
        progress: AgentProgress | None = None,
        **extra: Any,  # noqa: ANN401  # tracked: #288
    ) -> T: ...

    def progress(self, progress: AgentProgress) -> AbstractContextManager[None]: ...  # noqa: D102  # tracked: #288

    def snapshot_workspace(self, label: str) -> None: ...  # noqa: D102  # tracked: #288

    def trusted_input_changes(self) -> list[str]: ...  # noqa: D102  # tracked: #288

    def begin_completed_round(  # noqa: D102  # tracked: #288
        self,
        round_number: int,
        *,
        state_transition: StateTransition,
    ) -> None: ...

    def persist_completed_round(self) -> None: ...  # noqa: D102  # tracked: #288

    def publish_committed_state(  # noqa: D102
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None: ...

    def reselect_gpu(self) -> None: ...  # noqa: D102  # tracked: #288

    def wait_for_debug(self, step: str) -> None: ...  # noqa: D102  # tracked: #288

    def close(self) -> None: ...  # noqa: D102  # tracked: #288
