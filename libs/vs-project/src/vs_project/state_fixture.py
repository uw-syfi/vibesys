"""A state directory below a project's ``.vibesys`` layout, for fixtures that need no run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_project import _paths as project_paths
from vs_project._store_document import STORE_FILE, record_document

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api.state_store import StoreRecord
    from vs_project.project import Project


def scratch_state_directory(project_root: Path, name: str) -> Path:
    """Create and return the directory ``name`` below the project's state layout.

    For a test double that persists through a ``StateNamespace`` without a run record.
    The layout stays owned by this library; callers never spell the path.
    """
    directory = project_root / project_paths.STATE_DIRECTORY_PATH / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def stored_record_bytes(record: StoreRecord | None) -> bytes:
    """The bytes of the run's stored-record file holding ``record``, as the current format has them."""
    return record_document(record).encode()


def seed_stored_record(project: Project, run_id: str, record: StoreRecord | None) -> None:
    """Put ``record`` where a run saved before the step loop kept it, for a test of a reader.

    The library writes nothing there any more, so the fixture is the only writer.
    """
    seed_store_document(project, run_id, stored_record_bytes(record))


def seed_store_document(project: Project, run_id: str, contents: bytes) -> None:
    """Put ``contents`` in the run's stored-record file, including bytes that are not a document."""
    project.state.state_store_namespace(run_id).write_bytes(STORE_FILE, contents)
