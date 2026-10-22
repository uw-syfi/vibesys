"""Public aggregate for one repository-native VibeSys project."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Self

from vs_project._git_process import git_environment
from vs_project._layout import (
    ConfigurationRoot,
    ProjectLayout,
    ProjectLayoutError,
    TaskDirectory,
    TaskName,
    TasksRoot,
)
from vs_project._state import ProjectState, _state_home, validate_run_id
from vs_project._state_io import publish_run_link, read_retained_bytes
from vs_project._state_store import LocalStateStore
from vs_sim.api import ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_project._admission import FileAdmissionStore
    from vs_project._git_process import Git
    from vs_project._state_io import StoreDurabilityEffects
    from vs_project.api.state_store import (
        CommitFault,
        ObservationFault,
        StateStore,
        StateStoreFactory,
        StoreRecord,
    )


class Project:
    """One canonical project root with authored tasks and generated state."""

    def __init__(
        self, layout: ProjectLayout, *, state_stores: StateStoreFactory | None = None
    ) -> None:
        """Bind layout and state operations to the same validated root.

        ``state_stores`` opens each run's ``StateStore``; ``None`` opens the
        crash-atomic local one under the project's state directory.
        """
        self._layout = layout
        self._state_stores = state_stores
        self._state: ProjectState | None = None

    @classmethod
    def open(
        cls, project_root: Path | str, *, state_stores: StateStoreFactory | None = None
    ) -> Self:
        """Open an existing directory as a project.

        Authored task configuration and generated state may both be absent.
        Operations that need either surface validate it when called.
        """
        return cls(ProjectLayout.open(project_root), state_stores=state_stores)

    @classmethod
    def discover(cls, start: Path | str) -> Self:
        """Find the closest task-configured project at or above an existing path."""
        return cls(ProjectLayout.discover(start))

    @property
    def root(self) -> Path:
        """Return the absolute, resolved candidate repository root."""
        return self._layout.project_root.path

    @property
    def state(self) -> ProjectState:
        """Return state operations bound to this project's canonical root."""
        if self._state is None:
            self._state = ProjectState(self.root)
        return self._state

    def state_store(
        self,
        run_id: str,
        *,
        fault_plan: Iterable[CommitFault | None] = (),
        lease_fault_plan: Iterable[CommitFault | None] = (),
        observation_fault_plan: Iterable[ObservationFault | None] = (),
        effects: StoreDurabilityEffects | None = None,
    ) -> StateStore:
        """Open the shared atomic record and host fence for one validated run.

        Fault plans and ``effects`` (the durability operations; default the local
        filesystem) drive the local store only; a project opened with injected
        ``state_stores`` rejects them, since its stores take their faults at
        construction.
        """
        if self._state_stores is not None:
            if (
                tuple(fault_plan)
                or tuple(lease_fault_plan)
                or tuple(observation_fault_plan)
                or effects is not None
            ):
                message = "fault plans apply to the local state store, not injected state_stores"
                raise ValueError(message)
            return self._state_stores(self, run_id)
        return LocalStateStore(
            self,
            run_id,
            fault_plan=fault_plan,
            lease_fault_plan=lease_fault_plan,
            observation_fault_plan=observation_fault_plan,
            effects=effects,
        )

    def stored_record(self, run_id: str) -> StoreRecord | None:
        """Return the run's latest stored record, or ``None`` without creating any state.

        Opening a local store creates its directory, so a run that has no record
        yet is detected first.
        """
        if (
            self._state_stores is None
            and self.state.state_store_namespace(run_id).read_bytes("store.json") is None
        ):
            return None
        return self.state_store(run_id).load()

    @staticmethod
    def admissions(*, home: Path | None = None) -> FileAdmissionStore:
        """Open this host user's retained admissions, independent of any project root."""
        user_home = home or Path.home()
        return import_module("vs_project._admission").FileAdmissionStore(
            user_home / ".vibesys" / "admissions" / "v1", durable_root=user_home
        )

    def is_initialized(self) -> bool:
        """Return whether this project has repository-native task configuration."""
        return self._layout.is_initialized()

    def configuration_root(self) -> ConfigurationRoot:
        """Return the validated human-authored configuration root."""
        return self._layout.configuration_root()

    def configuration_path(self) -> Path:
        """Return the canonical configuration path, whether or not it exists."""
        return self._layout.configuration_path()

    def tasks_root(self) -> TasksRoot:
        """Return the validated root containing task definitions."""
        return self._layout.tasks_root()

    def discover_tasks(self) -> tuple[TaskDirectory, ...]:
        """Return all validated task definitions ordered by name."""
        return self._layout.discover_tasks()

    def select_task(self, task_name: TaskName | str | None = None) -> TaskDirectory:
        """Select a task explicitly, or implicitly when exactly one exists."""
        return self._layout.select_task(task_name)

    @classmethod
    def is_state_initialized(cls, path: Path | str) -> bool:
        """Return whether a directory contains initialized generated state."""
        return ProjectState.is_project_root(path)

    @classmethod
    def find_state_projects(cls, collection: Path | str) -> tuple[Path, ...]:
        """Return state-initialized projects directly below a collection."""
        return ProjectState.find_projects(collection)

    @staticmethod
    def validate_collection_root(collection: Path, *, git: Git) -> None:
        """Reject a collection whose child projects would nest inside a Git repository."""
        root = collection.expanduser().resolve()
        for ancestor in (root, *root.parents):
            if not ancestor.is_dir() or not (ancestor / ".git").exists():
                continue
            try:
                result = git.run_text(
                    ["rev-parse", "--show-toplevel"],
                    cwd=ancestor,
                    env=git_environment(safe_directory=ancestor),
                    timeout=10.0,
                )
            except (OSError, ProcessTimeoutError) as exc:
                message = f"could not validate project collection {root}: {exc}"
                raise ProjectLayoutError(message) from exc
            if result.returncode == 0:
                repository = Path(result.stdout.strip()).resolve()
                message = (
                    f"project collection {root} is inside Git repository {repository}; "
                    "each copied project must own its repository"
                )
                raise ProjectLayoutError(message)

    @classmethod
    def log_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return a run log destination, including before a root is materialized."""
        return ProjectState.log_directory_for(project_root, run_id)

    @classmethod
    def run_head_path_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return the writer's published head beside its canonical log."""
        return cls.log_directory_for(project_root, run_id) / "run.head"

    @classmethod
    def run_log_path_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return the transport log beside the authoritative JSONL journal."""
        return cls.log_directory_for(project_root, run_id) / "run.log"

    @classmethod
    def chat_session_directory_for(
        cls, project_root: Path | str, run_id: str, session_id: str
    ) -> Path:
        """A run-owned chat session, retained with the run's log directory."""
        return ProjectState.chat_session_directory_for(project_root, run_id, session_id)

    @classmethod
    def run_blobs_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return immutable run-transport content, beside the authoritative journal."""
        return cls.log_directory_for(project_root, run_id) / "blobs"

    @classmethod
    def event_journal_path_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return the authoritative core-event journal for one run."""
        return cls.log_directory_for(project_root, run_id) / "core-events.jsonl"

    def retained_event_journal(self, run_id: str, *, max_bytes: int) -> bytes | None:
        """Read the original bounded JSONL journal for a semantic restart, without writing."""
        return read_retained_bytes(
            self.event_journal_path_for(self.root, run_id), max_bytes=max_bytes
        )

    @classmethod
    def publish_run_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Atomically publish a host discovery link to this run's log directory."""
        validate_run_id(run_id)
        return publish_run_link(
            _state_home() / "runs" / run_id, cls.log_directory_for(project_root, run_id)
        )

    @staticmethod
    def indexed_run_directories() -> tuple[Path, ...]:
        """List all host discovery entries, including damaged and dangling entries.

        Maintenance callers must report unsupported entries rather than silently
        ignoring them. No state or index is created by this read.
        """
        index = _state_home() / "runs"
        return tuple(sorted(index.iterdir())) if index.is_dir() else ()

    @classmethod
    def logged_run_ids_for(cls, project_root: Path | str) -> tuple[str, ...]:
        """Return the ids of the runs with logs on this machine under ``project_root``, sorted.

        The log store is keyed by the project's root path, not by what the checkout
        records, so a run that never committed its manifest is still named here.
        """
        return ProjectState.logged_run_ids_for(project_root)

    @classmethod
    def log_streams_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return where a run's log store (``vs_logs``) keeps its streams.

        The run writes there and the host helper reads there; both ask here, so the
        layout has one owner.
        """
        return cls.log_directory_for(project_root, run_id) / "streams"

    @classmethod
    def agent_homes_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return the machine-local root of one run's dedicated agent CLI homes."""
        return ProjectState.agent_homes_directory_for(project_root, run_id)
