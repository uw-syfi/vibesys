"""Workspace setup-plan and copy-machinery tests.

The copy policies are declarative (``plan_setup`` returns CopySpec /
InputProjectSpec records), so most tests assert on the plan itself and
only the edge-case tests materialize files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import WorkspaceSource
from vibesys.run import CopySpec, InputProjectSpec, Workspace
from vibesys.run.workspace import GitSourceSpec
from vibesys.sandbox.run_environment import LocalEnvironment

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


class _StubRunEnvironment(LocalEnvironment):
    """LocalEnvironment with ``isolated`` under the test's control."""

    def __init__(self, *, isolated: bool) -> None:
        self.isolated = isolated


def _make_workspace(
    root: Path,
    *,
    isolated: bool = False,
    excluded_dirs: Iterable[str] | None = None,
    compute_backend: ComputeBackend | None = None,
) -> Workspace:
    return Workspace(
        root,
        run_environment=_StubRunEnvironment(isolated=isolated),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=root.parent,
        excluded_dirs=excluded_dirs if excluded_dirs is not None else {".git", "target"},
        compute_backend=compute_backend,
    )


def _write_platform_skill(root: Path) -> Path:
    """A skill carrying one references/platforms/<backend>/ dir per backend."""
    skill = root / "serving-systems"
    (skill / "references" / "algorithms").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# serving-systems\n")
    (skill / "references" / "algorithms" / "continuous-batching.md").write_text("# contract\n")
    for backend in ComputeBackend:
        plat = skill / "references" / "platforms" / backend.value
        plat.mkdir(parents=True)
        (plat / "floor.md").write_text(f"# {backend.value} floor\n")
    # Same-named dir outside references/platforms/ must survive.
    decoy = skill / "references" / "models" / "cuda"
    decoy.mkdir(parents=True)
    (decoy / "note.md").write_text("# decoy\n")
    return skill


@pytest.mark.parametrize(
    "selected", [ComputeBackend.CUDA, ComputeBackend.TRAINIUM, ComputeBackend.METAL]
)
def test_skill_copy_into_workspace_root_prunes_foreign_platforms(
    tmp_path: Path, selected: ComputeBackend
) -> None:
    """The workspace-root skill copy is what the implementer prompt points at.

    It must be pruned exactly like the per-CLI copies — otherwise the agent can
    open another platform's floor and apply guidance that is wrong for its
    hardware (eliminating KV padding is correct on cuda, inverted on trainium).
    """
    skill = _write_platform_skill(tmp_path / "src")
    ws = _make_workspace(tmp_path / "ws", compute_backend=selected)
    ws.create()

    ws.copy_dir(skill, ws.root / skill.name, prune_platforms=True)

    platforms = ws.root / skill.name / "references" / "platforms"
    assert {p.name for p in platforms.iterdir()} == {selected.value}
    # Portable tiers and same-named non-platform dirs are untouched.
    assert (ws.root / skill.name / "references/algorithms/continuous-batching.md").is_file()
    assert (ws.root / skill.name / "references/models/cuda/note.md").is_file()


def test_skill_copy_without_backend_keeps_every_platform(tmp_path: Path) -> None:
    skill = _write_platform_skill(tmp_path / "src")
    ws = _make_workspace(tmp_path / "ws", compute_backend=None)
    ws.create()

    ws.copy_dir(skill, ws.root / skill.name, prune_platforms=True)

    platforms = ws.root / skill.name / "references" / "platforms"
    assert {p.name for p in platforms.iterdir()} == {b.value for b in ComputeBackend}


def test_non_skill_copies_never_prune_platforms(tmp_path: Path) -> None:
    """prune_platforms is opt-in; an input bundle that happens to contain a
    references/platforms tree is copied verbatim."""
    src = _write_platform_skill(tmp_path / "src")
    ws = _make_workspace(tmp_path / "ws", compute_backend=ComputeBackend.CUDA)
    ws.create()

    ws.copy_dir(src, ws.root / "input")

    platforms = ws.root / "input" / "references" / "platforms"
    assert {p.name for p in platforms.iterdir()} == {b.value for b in ComputeBackend}


def test_every_skill_copy_step_is_marked_for_pruning(tmp_path: Path) -> None:
    """Each of the three skill CopySpecs (root, per-CLI refresh, fresh setup)
    must set prune_platforms; a new one added without it silently leaks."""
    ws = _make_workspace(tmp_path / "ws")
    skills = [tmp_path / "skills" / "serving-systems"]
    (ws.root / ".claude" / "skills" / "serving-systems").mkdir(parents=True)
    (ws.root / "serving-systems").mkdir(parents=True)

    plan = ws.plan_setup(
        existing=False,
        input_dir=tmp_path / "input",
        evaluator_source=None,
        skill_sources=skills,
        workspace_sources=(),
        input_project_dir=None,
        profiler_support_path=None,
        profiler_support_name=None,
    )

    skill_steps = [s for s in plan if isinstance(s, CopySpec) and s.src in skills]
    assert skill_steps, "expected skill copy steps in the plan"
    assert all(s.prune_platforms for s in skill_steps), [
        str(s.dest) for s in skill_steps if not s.prune_platforms
    ]


def test_fresh_plan_with_git_source_rejects_input_collisions(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path / "ws")
    input_dir = tmp_path / "input"
    evaluator = tmp_path / "evaluators" / "queue"
    skills = [tmp_path / "skills" / "serving-systems"]
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    plan = ws.plan_setup(
        existing=False,
        input_dir=input_dir,
        evaluator_source=evaluator,
        skill_sources=skills,
        workspace_sources=(source,),
        input_project_dir=input_dir,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
        extra_input_excludes=frozenset({"model"}),
    )

    assert plan == (
        GitSourceSpec(source=source),
        CopySpec(
            src=input_dir,
            dest=ws.root,
            extra_excludes=frozenset({"model"}),
            reject_collisions=True,
        ),
        CopySpec(
            src=evaluator,
            dest=ws.root / "_evaluator" / "queue",
            respect_gitignore=True,
            require_absent=ws.root / "_evaluator",
            require_absent_message=(
                "_evaluator is reserved for the manifest-declared evaluator source"
            ),
        ),
        CopySpec(src=skills[0], dest=ws.root / "serving-systems", prune_platforms=True),
        InputProjectSpec(project_dir=input_dir),
        CopySpec(src=tmp_path / "profilers" / "nsys", dest=ws.root / "nsys_profiler"),
    )


def test_fresh_plan_without_sources_does_not_reject_collisions(tmp_path: Path) -> None:
    ws = _make_workspace(tmp_path / "ws")
    input_dir = tmp_path / "input"

    plan = ws.plan_setup(
        existing=False,
        input_dir=input_dir,
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=None,
        profiler_support_name=None,
    )

    assert plan == (CopySpec(src=input_dir, dest=ws.root),)


def test_resume_plan_only_refreshes_skills_and_missing_profiler(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    skill = tmp_path / "skills" / "serving-systems"
    # Skill targets that exist in the interrupted workspace get refreshed —
    # both the workspace-root copy and the per-CLI mirror dirs.
    (root / "serving-systems").mkdir(parents=True)
    (root / ".claude" / "skills" / "serving-systems").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=True,
        input_dir=tmp_path / "input",
        evaluator_source=tmp_path / "evaluator",
        skill_sources=[skill],
        input_project_dir=tmp_path / "input",
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
    )

    # No input/evaluator/input-project copies on resume.
    assert plan == (
        CopySpec(src=skill, dest=root / "serving-systems", prune_platforms=True),
        CopySpec(
            src=skill, dest=root / ".claude" / "skills" / "serving-systems", prune_platforms=True
        ),
        CopySpec(src=tmp_path / "profilers" / "nsys", dest=root / "nsys_profiler"),
    )


def test_resume_plan_skips_profiler_already_present(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "nsys_profiler").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=True,
        input_dir=tmp_path / "input",
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
    )

    assert plan == ()


def test_setup_rejects_preexisting_evaluator_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    (evaluator / "checker.py").write_text("pass\n")
    # The input bundle smuggled in its own _evaluator dir.
    (root / "_evaluator").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=False,
        input_dir=tmp_path / "input",
        evaluator_source=evaluator,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=None,
        profiler_support_name=None,
    )
    evaluator_steps = plan[1:]  # skip the input copy; input dir is absent

    with pytest.raises(ValueError, match="_evaluator is reserved"):
        ws.setup(evaluator_steps, existing=False)


def test_setup_prunes_excluded_dirs_on_fresh_runs_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "target").mkdir(parents=True)
    (root / "target" / "stale.o").write_text("stale")

    ws = _make_workspace(root)
    ws.setup((), existing=True)
    assert (root / "target" / "stale.o").exists()

    ws.setup((), existing=False)
    assert not (root / "target").exists()


def test_copy_dir_replaces_external_symlinks_when_not_isolated(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "model").symlink_to(outside)

    dst = tmp_path / "ws"
    _make_workspace(dst, isolated=False).copy_dir(src, dst)

    assert not (dst / "model").exists()
    assert (dst / "model.symlink_target").read_text() == str(outside.resolve())


def test_copy_dir_removes_external_symlinks_when_isolated(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "model").symlink_to(outside)
    (src / "kept.py").write_text("pass\n")

    dst = tmp_path / "ws"
    _make_workspace(dst, isolated=True).copy_dir(src, dst)

    assert not (dst / "model").exists()
    assert not (dst / "model.symlink_target").exists()
    assert (dst / "kept.py").is_file()
