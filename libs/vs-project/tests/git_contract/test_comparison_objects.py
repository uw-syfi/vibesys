"""Revision comparisons must not import build artifacts into the Git object store."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from vs_project.api.testing import ContractProject

REQUIRES_ON_DISK_REPOSITORY = True


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("ignored", [False, True])
def test_comparison_does_not_write_candidate_blobs(
    sandbox: ContractProject, *, tracked: bool, ignored: bool
) -> None:
    head = sandbox.start({"source.txt": "original source\n"})
    sandbox.repo.add_excludes(["target/"])
    path = "source.txt" if tracked else "target/build-output"
    sandbox.write(path, "unique changed content that must not become an object\n")
    objects = sandbox.root / ".git" / "objects"
    before = {p.relative_to(objects) for p in objects.rglob("*") if p.is_file()}
    assert sandbox.repo.worktree_matches(head, ["."], include_ignored=ignored) is (
        not tracked and not ignored
    )
    after = {p.relative_to(objects) for p in objects.rglob("*") if p.is_file()}
    assert after == before
