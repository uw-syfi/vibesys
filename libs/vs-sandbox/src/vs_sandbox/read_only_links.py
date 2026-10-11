"""Reject operator read-only paths whose symlinks would dangle inside a confined job.

A confined job sees only the workspace, the system directories, and the paths
the operator lists as read-only. A symlink inside a listed tree is followed by
the job's kernel, so it works only if every hop of its chain is visible too. A
Python venv is the usual case: ``bin/python`` links to an alias directory that
links to the real interpreter install. List the venv alone and the job fails
with ``No such file`` for the interpreter, far from the setting that caused it.

This check runs when the run starts and rejects such a listing, naming the
listed path, the link, and the target the operator must also list. It never
exposes anything itself: what a job can see stays exactly what the operator
wrote down.

What is checked: every symlink that sits directly in a ``bin`` or ``sbin``
directory of a listed tree, that is, the entry points an executable is
reached through. Other symlinks (documentation, data) are not followed, so a
tree with an unrelated dangling link is still accepted.
"""

from __future__ import annotations

import os
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_sandbox.host_sandbox import system_read_roots

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_sandbox.host_resources import HostResource

#: The kernel's limit on symlinks followed in one lookup (``ELOOP`` beyond it).
_MAX_HOPS = 40
_EXECUTABLE_DIRS = frozenset({"bin", "sbin"})


class ReadOnlyPathLinkError(ValueError):
    """A listed read-only path holds a symlink whose chain the job cannot follow."""


@dataclass(frozen=True)
class _Problem:
    listed: str
    link: str
    target: str
    reason: str
    fix: str


def check_read_only_links(
    key: str,
    read_only_paths: Sequence[str],
    *,
    workspace: Path,
    resources: Iterable[HostResource],
) -> None:
    """Raise when an entry-point symlink under *read_only_paths* would dangle in a job.

    *key* names the setting in messages. *resources* are all the host resources
    the job's confinement binds (the listed paths included); with *workspace*
    and the system directories they are everything the job can see.

    Raises:
        ReadOnlyPathLinkError: Naming each listed path, the link in it, and the
            path to add to the setting (or the link to repair).
    """
    visible = _Visible(
        roots=tuple(
            sorted(
                {
                    *system_read_roots(),
                    str(workspace),
                    *(str(resource.path) for resource in resources),
                    *read_only_paths,
                }
            )
        )
    )
    problems = [
        problem
        for listed in read_only_paths
        for link in _entry_point_links(listed)
        if (problem := _first_problem(key, listed, link, visible)) is not None
    ]
    if problems:
        lines = [
            f"{key} lists paths holding symlinks that a confined job could not follow, "
            "so the job would fail with 'No such file':"
        ]
        lines += [f"  {p.listed}: {p.link} -> {p.target} {p.reason}; {p.fix}" for p in problems]
        raise ReadOnlyPathLinkError("\n".join(lines))


@dataclass(frozen=True)
class _Visible:
    roots: tuple[str, ...]

    def covers(self, path: str) -> bool:
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in self.roots)

    def is_ancestor(self, path: str) -> bool:
        return any(root.startswith(path.rstrip("/") + "/") for root in self.roots)

    def is_root(self, path: str) -> bool:
        return path in self.roots


def _entry_point_links(listed: str) -> list[str]:
    """Return the symlinks directly inside ``bin``/``sbin`` directories of *listed*."""
    links: list[str] = []
    for directory, subdirectories, files in os.walk(listed, followlinks=False):
        if PurePosixPath(directory).name in _EXECUTABLE_DIRS:
            links += sorted(
                str(PurePosixPath(directory) / name)
                for name in (*subdirectories, *files)
                if (Path(directory) / name).is_symlink()
            )
    return links


def _first_problem(key: str, listed: str, link: str, visible: _Visible) -> _Problem | None:
    """Walk *link* as the job's kernel would; describe the first hop it cannot take."""
    # Linux binds a listed path by what it resolves to, so a listed symlink is
    # the directory it points to. Seatbelt checks the resolved path instead.
    dereferenced = sys.platform != "darwin"
    target = str(Path(link).readlink())
    pending = target.split("/")
    current = PurePosixPath("/" if target.startswith("/") else PurePosixPath(link).parent)
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        step = current / part
        if visible.covers(str(step)):
            host = Path(step)
            if host.is_symlink() and not (dereferenced and visible.is_root(str(step))):
                hops += 1
                if hops > _MAX_HOPS:
                    return _Problem(listed, link, target, "loops", f"repair {link}")
                text = str(host.readlink())
                pending = [*text.split("/"), *pending]
                if text.startswith("/"):
                    current = PurePosixPath("/")
                continue
            if not (host.exists() or host.is_symlink()):
                needed = posixpath.normpath(str(step.joinpath(*pending)))
                return _Problem(
                    listed, link, target, f"is broken ({needed} does not exist)", f"repair {link}"
                )
            current = step
        elif visible.is_ancestor(str(step)):
            current = step
        else:
            needed = posixpath.normpath(str(step.joinpath(*pending)))
            return _Problem(
                listed,
                link,
                target,
                f"reaches {needed}, which the job cannot see",
                f"add {needed} (or the directory that holds it) to {key}",
            )
    return None
