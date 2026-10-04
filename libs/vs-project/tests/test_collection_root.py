"""Project collections reject repositories, rather than unvalidated Git markers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_project.api import Project, ProjectLayoutError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("marker_kind", ["empty-directory", "invalid-file"])
@pytest.mark.parametrize("depth", range(4))
def test_non_repository_git_marker_allows_collection(
    tmp_path: Path, marker_kind: str, depth: int
) -> None:
    parent = tmp_path / "not-a-repository"
    parent.mkdir()
    marker = parent / ".git"
    if marker_kind == "empty-directory":
        marker.mkdir()
    else:
        marker.write_text("not Git metadata\n")
    collection = parent.joinpath(*[f"missing-{number}" for number in range(depth)])

    Project.validate_collection_root(collection)

    assert marker.exists()
    assert collection.exists() == (depth == 0)


@pytest.mark.parametrize("marker_kind", ["empty-directory", "invalid-file"])
def test_invalid_inner_git_marker_cannot_hide_containing_repository(
    tmp_path: Path, marker_kind: str
) -> None:
    repository = tmp_path / "repository"
    run_test_command(["git", "init", "-q", str(repository)], check=True)
    inner = repository / "inner"
    inner.mkdir()
    marker = inner / ".git"
    if marker_kind == "empty-directory":
        marker.mkdir()
    else:
        marker.write_text("not Git metadata\n")

    with pytest.raises(ProjectLayoutError, match=str(repository)):
        Project.validate_collection_root(inner / "missing" / "collection")

    assert marker.exists()
