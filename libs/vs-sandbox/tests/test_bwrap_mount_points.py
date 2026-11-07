"""Bubblewrap can create every resource's mount point, whatever links the bound trees hold.

Bubblewrap creates a bind's mount point by walking its destination inside the
new root, where each tree bound earlier shows its host source's entries. A
symlink met on that walk is looked up before the new root is entered, so an
absolute target never resolves there and the launch aborts with ``Can't mkdir``
(#4028). The helpers below model that walk, and the links a confined process
then follows, over the argv :class:`HostSandbox` emits; no real bubblewrap runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_sandbox.api import HostSandbox

_BINDS = frozenset({"--ro-bind", "--ro-bind-try", "--bind", "--bind-try"})
#: The kernel's limit on symlinks followed in one lookup.
_MAX_LINKS = 40

_Mounts = dict[Path, Path]


def _binds(argv: list[str]) -> list[tuple[Path, Path]]:
    """Return each host bind in *argv* as ``(source, destination)``, in the order applied."""
    prefix = argv[: argv.index("--")]
    return [
        (Path(prefix[index + 1]), Path(prefix[index + 2]))
        for index, flag in enumerate(prefix)
        if flag in _BINDS
    ]


def _shown(step: Path, mounts: _Mounts) -> tuple[Path | None, bool]:
    """Return the host entry the namespace shows at *step*, and whether *step* is a mount point.

    The entry is ``None`` when no mount holds *step*: there it is at most a
    directory bubblewrap created on the way to a deeper mount point.
    """
    holders = [point for point in mounts if point == step or point in step.parents]
    if not holders:
        return None, False
    point = max(holders, key=lambda holder: len(holder.parts))
    return mounts[point] / step.relative_to(point), point == step


def _uncreatable(argv: list[str]) -> list[Path]:
    """Return the bind destinations whose walk meets a symlink an earlier mount shows."""
    mounts: _Mounts = {}
    failed: list[Path] = []
    for source, destination in _binds(argv):
        for step in (*reversed(destination.parents[:-1]), destination):
            host, is_mount_point = _shown(step, mounts)
            if host is not None and not is_mount_point and host.is_symlink():
                failed.append(destination)
                break
        mounts[destination] = source
    return failed


def _reached(path: Path, argv: list[str]) -> Path | None:
    """Return the host directory *path* names inside the namespace, or ``None`` if it dangles."""
    mounts: _Mounts = {destination: source for source, destination in _binds(argv)}
    pending = list(path.parts[1:])
    current = Path("/")
    links = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            continue
        step = current / part
        host, is_mount_point = _shown(step, mounts)
        if host is None:
            if not any(step in point.parents for point in mounts):
                return None
        elif not is_mount_point and host.is_symlink():
            links += 1
            if links > _MAX_LINKS:
                return None
            target = host.readlink()
            pending = [*target.parts[1 if target.is_absolute() else 0 :], *pending]
            if target.is_absolute():
                current = Path("/")
            continue
        elif not host.exists():
            return None
        current = step
    host, _ = _shown(current, mounts)
    return None if host is None else host.resolve()


def _wrap(root: Path, *, read: tuple[Path, ...], write: tuple[Path, ...] = ()) -> list[str]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    sandbox = HostSandbox(
        workspace=workspace,
        bwrap_path="bwrap",
        system_read_roots=(str(root / "usr"), str(root / "etc")),
        read_paths=read,
        write_paths=write,
    )
    return sandbox.wrap(["true"])


@pytest.mark.regression
def test_a_path_entry_through_an_alternatives_link_binds_where_it_resolves(tmp_path: Path) -> None:
    """#4028: ``/usr/local/cuda-12/bin`` on PATH, through ``/etc/alternatives``, aborted bwrap."""
    real = tmp_path / "usr" / "local" / "cuda-12.8"
    (real / "bin").mkdir(parents=True)
    alternatives = tmp_path / "etc" / "alternatives"
    alternatives.mkdir(parents=True)
    (alternatives / "cuda-12").symlink_to(real)
    (tmp_path / "usr" / "local" / "cuda-12").symlink_to(alternatives / "cuda-12")
    entry = tmp_path / "usr" / "local" / "cuda-12" / "bin"

    argv = _wrap(tmp_path, read=(entry,))

    assert _uncreatable(argv) == []
    assert (real / "bin", real / "bin") in _binds(argv)
    assert _reached(entry, argv) == real / "bin"


def test_a_listed_link_outside_every_bound_tree_still_binds_under_its_own_name(
    tmp_path: Path,
) -> None:
    """The documented Linux behavior: a listed alias shows as the directory it points to."""
    real = tmp_path / "store" / "cpython-3.12.15"
    (real / "bin").mkdir(parents=True)
    alias = tmp_path / "store" / "cpython-3.12"
    alias.symlink_to(real.name)

    argv = _wrap(tmp_path, read=(alias,))

    assert (alias, alias) in _binds(argv)
    assert _reached(alias / "bin", argv) == real / "bin"


_TREES = ("usr", "etc", "data")


@dataclass(frozen=True)
class _Hop:
    """One link on the chain to the resource's content: the tree it sits in, and its target's form."""

    tree: str
    absolute: bool


@dataclass(frozen=True)
class _Layout:
    """A resource reached through links (absolute or relative) in shown or unseen trees.

    ``usr`` and ``etc`` are system roots; ``data`` is shown only when listed
    before the resource. The resource is ``<first hop>/bin``, or the first hop
    itself, and its content lives in ``usr`` or ``data``.
    """

    content_tree: str
    hops: tuple[_Hop, ...]
    data_listed: bool
    declare_the_link: bool
    writable: bool

    def shown(self, tree: str) -> bool:
        return tree != "data" or self.data_listed

    def build(self, root: Path) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
        """Create the layout under *root*; return the resource and the read and write paths."""
        for tree in _TREES:
            (root / tree).mkdir()
        content = root / self.content_tree / "content"
        (content / "bin").mkdir(parents=True)
        links = [root / hop.tree / f"link-{index}" for index, hop in enumerate(self.hops)]
        successors = [*links[1:], content]
        for link, successor, hop in zip(links, successors[: len(links)], self.hops, strict=True):
            link.symlink_to(successor if hop.absolute else os.path.relpath(successor, link.parent))
        entry = links[0] if links else content
        resource = entry if self.declare_the_link else entry / "bin"
        listed = (root / "data",) if self.data_listed else ()
        if self.writable:
            return resource, listed, (resource,)
        return resource, (*listed, resource), ()


_LAYOUTS = st.builds(
    _Layout,
    content_tree=st.sampled_from(("usr", "data")),
    hops=st.lists(
        st.builds(_Hop, tree=st.sampled_from(_TREES), absolute=st.booleans()), max_size=3
    ).map(tuple),
    data_listed=st.booleans(),
    declare_the_link=st.booleans(),
    writable=st.booleans(),
)
_ISSUE_4028 = _Layout(
    content_tree="usr",
    hops=(_Hop("usr", absolute=True), _Hop("etc", absolute=True)),
    data_listed=False,
    declare_the_link=False,
    writable=False,
)


@pytest.mark.regression
@given(layout=_LAYOUTS)
@example(layout=_ISSUE_4028)
def test_every_mount_point_is_creatable_and_every_resource_reachable(
    tmp_path_factory: pytest.TempPathFactory, layout: _Layout
) -> None:
    """Bubblewrap never aborts, and the resource's content is reachable inside the namespace."""
    root = tmp_path_factory.mktemp("mounts")
    resource, read, write = layout.build(root)

    argv = _wrap(root, read=read, write=write)

    assert _uncreatable(argv) == []
    expected = resource.resolve()
    assert expected in (_reached(resource, argv), _reached(expected, argv))
    if not layout.hops or not layout.shown(layout.hops[0].tree):
        # Bubblewrap could already bind it there, so it keeps its own name.
        assert any(destination == resource for _, destination in _binds(argv))
    hidden_after_shown = any(
        layout.shown(earlier.tree) and not layout.shown(later.tree)
        for index, earlier in enumerate(layout.hops)
        for later in layout.hops[index + 1 :]
    )
    if not hidden_after_shown:
        # Every link after a shown one is shown too, so its own name reaches it.
        assert _reached(resource, argv) == expected
