"""Run discovery addresses, loaded only when Project publishes a run."""

from pathlib import Path

from vs_project._state_io import _atomic_write_bytes
from vs_proto.api import RunAddress, validate


def publish_address(path: Path, directory: Path, project_root: Path) -> None:
    """Publish validated routing metadata before a discovery link can expose the run."""
    address = RunAddress(project_root=str(project_root), log_directory=str(directory.resolve()))
    validate(address)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_write_bytes(path, address.to_binary())
