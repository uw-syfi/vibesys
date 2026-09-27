"""Public contracts for canonical project-tree materialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vs_runtime.api.infrastructure import (
    FreshProjectError,
    FreshProjectErrorKind,
    GitSourceMaterialization,
    ProjectMaterializer,
    ProjectTreeCopy,
    SDKRoots,
    WorkspaceSourceValue,
)
from vs_runtime.api.testing import FakeGitRunner, FakeProjectMaterializationEffects


def _materializer(
    root: Path,
    *,
    effects: FakeProjectMaterializationEffects | None = None,
    git: FakeGitRunner | None = None,
    excluded: frozenset[str] = frozenset({".git", "target"}),
) -> ProjectMaterializer:
    return ProjectMaterializer(
        root,
        effects=effects or FakeProjectMaterializationEffects(),
        log=lambda _message: None,
        sdk_roots=SDKRoots(checkout=root.parent / "sdk", packaged=root.parent / "sdk"),
        excluded_dirs=excluded,
        git_runner=git,
    )


def _source(*, dest: str = "library", strip_git: bool = True) -> GitSourceMaterialization:
    return GitSourceMaterialization(
        name="library",
        source=WorkspaceSourceValue(
            repo="https://example.invalid/library.git",
            commit="0123456",
            dest=dest,
            strip_git=strip_git,
        ),
    )


def _write_partial_then_fail(destination: Path) -> None:
    (destination / "partial.txt").write_text("partial")
    message = "finalization failed"
    raise RuntimeError(message)


def test_copy_tree_replaces_children_but_preserves_excluded_mounts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "new.py").write_text("new\n")
    destination = tmp_path / "workspace"
    (destination / "old").mkdir(parents=True)
    (destination / "target").mkdir()
    (destination / "target" / "keep.o").write_text("keep")
    materializer = _materializer(destination)

    materializer.copy_tree(ProjectTreeCopy(src=source, dest=destination))

    assert sorted(path.name for path in destination.iterdir()) == ["new.py", "target"]
    assert (destination / "target" / "keep.o").read_text() == "keep"


def test_copy_tree_applies_exact_relative_path_exclusions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    excluded = source / "references" / "platforms" / "rocm"
    excluded.mkdir(parents=True)
    (excluded / "floor.md").write_text("hidden")
    decoy = source / "references" / "models" / "rocm"
    decoy.mkdir(parents=True)
    (decoy / "note.md").write_text("kept")
    destination = tmp_path / "workspace"

    _materializer(destination).copy_tree(
        ProjectTreeCopy(
            src=source,
            dest=destination,
            excluded_relative_paths=frozenset({Path("references/platforms/rocm")}),
        )
    )

    assert not (destination / "references" / "platforms" / "rocm").exists()
    assert (destination / "references" / "models" / "rocm" / "note.md").is_file()


@pytest.mark.parametrize("isolated", [False, True], ids=["host", "isolated"])
def test_copy_tree_normalizes_external_symlinks(tmp_path: Path, *, isolated: bool) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "model").symlink_to(outside)
    destination = tmp_path / "workspace"

    _materializer(
        destination,
        effects=FakeProjectMaterializationEffects(isolated=isolated),
    ).copy_tree(ProjectTreeCopy(src=source, dest=destination))

    assert not (destination / "model").exists()
    marker = destination / "model.symlink_target"
    assert marker.exists() is not isolated
    if not isolated:
        assert marker.read_text() == str(outside.resolve())


def test_materialize_rejects_reserved_destination_before_copy(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    reserved = root / "_evaluator"
    reserved.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ValueError, match="reserved"):
        _materializer(root).materialize(
            (
                ProjectTreeCopy(
                    src=source,
                    dest=reserved / "checker",
                    require_absent=reserved,
                    require_absent_message="reserved evaluator path",
                ),
            ),
            existing=True,
        )


def test_fresh_project_removes_partial_destination_on_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "runs" / "workspace"
    materializer = _materializer(destination)

    with (
        pytest.raises(RuntimeError, match="finalization failed"),
        materializer.fresh_project(source, destination),
    ):
        _write_partial_then_fail(destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    ("destination", "materializer_root", "expected"),
    [
        ("source/child", "source/child", FreshProjectErrorKind.DESTINATION_INSIDE_SOURCE),
        ("existing", "existing", FreshProjectErrorKind.DESTINATION_EXISTS),
        ("destination", "other", FreshProjectErrorKind.ROOT_MISMATCH),
    ],
)
def test_fresh_project_rejects_unsafe_destination(
    tmp_path: Path,
    destination: str,
    materializer_root: str,
    expected: FreshProjectErrorKind,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    resolved_destination = tmp_path / destination
    if expected is FreshProjectErrorKind.DESTINATION_EXISTS:
        resolved_destination.mkdir()

    with (
        pytest.raises(FreshProjectError) as info,
        _materializer(tmp_path / materializer_root).fresh_project(
            source,
            resolved_destination,
        ),
    ):
        pass

    assert info.value.kind is expected


def test_relocate_copied_tree_removes_original_copy(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    evaluator = source_root / "checks" / "trusted"
    evaluator.mkdir(parents=True)
    (evaluator / "check.py").write_text("print('ok')\n")
    destination = tmp_path / "workspace"
    copied_evaluator = destination / "checks" / "trusted"
    copied_evaluator.mkdir(parents=True)
    (copied_evaluator / "stale.py").write_text("stale\n")
    canonical = destination / "_evaluator" / "trusted"

    _materializer(destination).relocate_copied_tree(
        ProjectTreeCopy(src=evaluator, dest=canonical),
        copied_from=source_root,
    )

    assert not copied_evaluator.exists()
    assert (canonical / "check.py").read_text() == "print('ok')\n"


def test_remove_paths_rejects_project_root_and_external_paths(tmp_path: Path) -> None:
    destination = tmp_path / "workspace"
    destination.mkdir()
    materializer = _materializer(destination)

    for path in (destination, tmp_path / "outside"):
        with pytest.raises(ValueError, match="escapes project root"):
            materializer.remove_paths((path,))


def test_repair_is_delegated_to_environment_effects(tmp_path: Path) -> None:
    effects = FakeProjectMaterializationEffects()
    materializer = _materializer(tmp_path / "workspace", effects=effects)

    materializer.repair()

    assert effects.repaired == [tmp_path / "workspace"]


def test_git_source_rejects_escape_and_excluded_destinations(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    materializer = _materializer(root, git=FakeGitRunner(head="0123456"))

    with pytest.raises(ValueError, match="escapes workspace"):
        materializer.materialize_git_source(_source(dest="link/library"))
    with pytest.raises(ValueError, match="excluded path component"):
        materializer.materialize_git_source(_source(dest="target/library"))


def test_git_source_rejects_checkout_that_differs_from_pin(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    git = FakeGitRunner(head="fedcba9")

    with pytest.raises(RuntimeError, match="checked out fedcba9, expected 0123456"):
        _materializer(root, git=git).materialize_git_source(_source())

    assert [args[0] for args, _cwd in git.calls] == ["clone", "checkout", "rev-parse"]
    assert not (root / "_vibesys_sources.json").exists()


def test_git_source_records_resolved_commit_and_strips_metadata(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    materializer = _materializer(root, git=FakeGitRunner(head="0123456789abcdef"))

    materializer.materialize_git_source(_source())

    assert not (root / "library" / ".git").exists()
    assert json.loads((root / "_vibesys_sources.json").read_text()) == [
        {
            "name": "library",
            "repo": "https://example.invalid/library.git",
            "commit": "0123456789abcdef",
            "requested_commit": "0123456",
            "dest": "library",
            "strip_git": True,
        }
    ]
