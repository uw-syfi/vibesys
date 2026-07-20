"""The run-context surface loops are allowed to depend on.

``LoopContext`` captures what loop implementations actually consume from
``_RunContext`` (verified by grepping ``ctx.`` usage across ``loops/`` and
``main.py``).  Loop entry points type against this protocol instead of the
concrete class, which keeps the facade's construction internals out of the
loops' contract.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from vibesys.agents.progress import AgentProgress
from vibesys.constants import ComputeBackend
from vibesys.profilers import ProfilerKind
from vibesys.run.git_tracker import GitTracker

T = TypeVar("T", bound=BaseModel)


class LoopContext(Protocol):
    # -- run identity / configuration -----------------------------------------
    backend: ComputeBackend
    model_name: str
    git_tracking: bool
    profiler_kind: ProfilerKind
    ref_name: str

    # -- collaborators --------------------------------------------------------
    supervisor: Any
    agent_runner: Any
    judge_backend: Any
    run_environment: Any
    run_environment_view: Any
    git: GitTracker

    # -- paths ----------------------------------------------------------------
    @property
    def exp_dir(self) -> Path: ...

    @property
    def log_dir(self) -> Path: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def run_log_path(self) -> Path: ...

    # -- agent-facing commands ------------------------------------------------
    @property
    def judge_accuracy_command(self) -> str | None: ...

    @property
    def judge_benchmark_command(self) -> str | None: ...

    @property
    def profiler_benchmark_command(self) -> str | None: ...

    # -- services -------------------------------------------------------------
    def lprint(self, text: str) -> None: ...

    def switch_log_file(self, label: int | str) -> None: ...

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
    ) -> T: ...

    def progress(self, progress: AgentProgress) -> AbstractContextManager[None]: ...

    def snapshot_workspace(self, label: str) -> None: ...

    def trusted_input_changes(self) -> list[str]: ...

    def reselect_gpu(self) -> None: ...

    def wait_for_debug(self, step: str) -> None: ...

    def close(self) -> None: ...
