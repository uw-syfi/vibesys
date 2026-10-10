"""State access stays inside its namespace: no symlink or ``..`` reaches outside it."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import Project, ProjectStateError

_NAME = st.text(alphabet="abcdefgh", min_size=1, max_size=4)
_PARTS = st.lists(_NAME, min_size=1, max_size=3, unique=True)


def _listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@given(parts=_PARTS, data=st.data(), points_outside=st.booleans())
@settings(max_examples=60)
def test_a_symlink_planted_at_any_depth_is_never_followed(
    parts: list[str], data: st.DataObject, *, points_outside: bool
) -> None:
    """Every directory from the project root down to the file, and the file itself."""
    with TemporaryDirectory() as scratch:
        base = Path(scratch)
        project_root = base / "project"
        outside = base / "outside"
        project_root.mkdir()
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside")
        project = Project.open(project_root)
        namespace = project.state.state_store_namespace("run-1")
        relative = "/".join(parts)
        namespace.write_bytes(relative, b"inside")

        file_path = namespace.external_directory().joinpath(*parts)
        chain = [file_path, *file_path.parents]
        chain = [
            path for path in chain if path.is_relative_to(project.root) and path != project.root
        ]
        victim = data.draw(st.sampled_from(chain))
        moved = victim.with_name(victim.name + ".moved")
        victim.rename(moved)
        victim.symlink_to(outside if points_outside else moved)
        outside_before = _listing(outside)

        with pytest.raises(ProjectStateError):
            namespace.read_bytes(relative)
        with pytest.raises(ProjectStateError):
            namespace.write_bytes(relative, b"planted")
        assert _listing(outside) == outside_before
        assert (outside / "secret").read_bytes() == b"outside"
        if points_outside:
            with pytest.raises(ProjectStateError, match="escapes"):
                namespace.read_bytes(relative)


@given(parts=_PARTS, data=st.data())
@settings(max_examples=40)
def test_parent_components_are_rejected_at_any_position(
    parts: list[str], data: st.DataObject
) -> None:
    with TemporaryDirectory() as scratch:
        namespace = Project.open(Path(scratch)).state.state_store_namespace("run-1")
        position = data.draw(st.integers(min_value=0, max_value=len(parts)))
        hostile = "/".join([*parts[:position], "..", *parts[position:]])
        with pytest.raises(ProjectStateError):
            namespace.read_bytes(hostile)
        with pytest.raises(ProjectStateError):
            namespace.write_bytes(hostile, b"x")
        with pytest.raises(ProjectStateError):
            namespace.external_directory(hostile)
