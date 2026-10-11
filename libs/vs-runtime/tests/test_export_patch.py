"""Patch export reads the canonical patch of a retained revision and changes nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from vs_core.api import ExportPatchBase, RevisionId, RevisionRef
from vs_runtime.api.core import ExecutionContext, ExportPatchOwner, NotApplied
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces

if TYPE_CHECKING:
    from pathlib import Path


class Export(ExportPatchBase):
    kind: Literal["demo.export_patch"] = "demo.export_patch"


def _ref(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


def _commit_of(ref: RevisionRef) -> str | None:
    root = ref.revision_id.root
    return root if ref.digest == f"git-commit:{root}" else None


def _context() -> ExecutionContext:
    return cast("ExecutionContext", None)


def _owner(root: Path, retained: set[str]) -> tuple[ExportPatchOwner, FakeWorkspaces]:
    workspaces = FakeWorkspaces(FakeWorkspace(path=root, known_revisions=retained))
    return ExportPatchOwner(workspaces, workspaces, _commit_of), workspaces


async def test_a_retained_revision_exports_its_patch(tmp_path: Path) -> None:
    owner, workspaces = _owner(tmp_path, {"r1"})
    workspaces.set_patch("r1", "diff --git a/x b/x\n")

    outcome = await owner.execute(Export(revision=_ref("r1")), _context())

    assert outcome == {"status": "succeeded", "patch": "diff --git a/x b/x\n"}


async def test_a_revision_the_run_does_not_retain_is_refused(tmp_path: Path) -> None:
    owner, workspaces = _owner(tmp_path, set())

    outcome = await owner.execute(Export(revision=_ref("gone")), _context())

    assert outcome["status"] == "rejected"
    assert workspaces.export_patch_calls == []


async def test_a_reference_that_names_no_commit_is_refused(tmp_path: Path) -> None:
    owner, _ = _owner(tmp_path, {"r1"})
    odd = RevisionRef(revision_id=RevisionId(root="r1"), digest="other:r1")

    assert (await owner.execute(Export(revision=odd), _context()))["status"] == "rejected"


async def test_inspection_proves_nothing_happened(tmp_path: Path) -> None:
    owner, _ = _owner(tmp_path, {"r1"})

    assert isinstance(await owner.inspect(Export(revision=_ref("r1")), _context()), NotApplied)
