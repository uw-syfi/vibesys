"""Convert host-resource declarations into validated sandbox import paths."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from vs_sandbox.host_resources import HostResource, HostResourceAccess


@dataclass(frozen=True)
class HostResourceImports:
    """Validated paths partitioned by the access a backend must grant."""

    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()


def _is_ancestor(ancestor: Path, path: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def prepare_host_resource_imports(
    workspace: Path,
    declarations: Iterable[HostResource],
    *,
    log: Callable[[str], None],
) -> HostResourceImports:
    """Validate declarations and partition them for an import backend.

    Paths must exist and be absolute. Canonical targets are checked so a
    symlink cannot expose a workspace ancestor and sibling runs. If the same
    path requests both access modes, read-write wins.
    """
    workspace = workspace.resolve()
    read_paths: dict[Path, None] = {}
    write_paths: dict[Path, None] = {}

    for resource in declarations:
        path = resource.path.expanduser()
        if not path.is_absolute():
            log(
                f"[host-resources] ignoring non-absolute {resource.purpose} path {path}; "
                "host resources must use absolute paths."
            )
            continue
        if not path.exists():
            continue

        resolved = path.resolve()
        if _is_ancestor(resolved, workspace):
            log(
                f"[host-resources] ignoring {resource.purpose} path {path} because it "
                "is an ancestor of the workspace; allowing it would expose sibling runs."
            )
            continue

        if resource.access is HostResourceAccess.READ_WRITE:
            read_paths.pop(path, None)
            write_paths[path] = None
        elif path not in write_paths:
            read_paths[path] = None

    return HostResourceImports(tuple(read_paths), tuple(write_paths))
