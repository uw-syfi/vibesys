"""Atomic host-wide discovery links for local run log directories."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from vs_project._state import _state_home, validate_run_id
from vs_project.errors import ProjectStateError


def publish_run_link(run_id: str, directory: Path) -> Path:
    """Publish an absolute target, atomically replacing only an existing symlink."""
    validate_run_id(run_id)
    index = _state_home() / "runs"
    index.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = index / run_id
    if link.exists() and not link.is_symlink():
        message = f"run index entry is not a symlink: {link}"
        raise ProjectStateError(message)
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".publish-", dir=index) as temporary:
        staged = Path(temporary) / "link"
        staged.symlink_to(directory.resolve(), target_is_directory=True)
        staged.replace(link)
    return link
