"""Application policy for canonical workspace-materialization plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibesys.constants import ComputeBackend
from vibesys.evaluators.input_manifest import WorkspaceSource
from vibesys.run.workspace_policy import (
    build_workspace_materialization_plan,
    materialization_source,
    skill_copy,
)
from vs_runtime.api.infrastructure import InputProjectMaterialization, ProjectTreeCopy


def _write_platform_skill(root: Path) -> Path:
    skill = root / "serving-systems"
    (skill / "references" / "algorithms").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# serving-systems\n")
    for backend in ComputeBackend:
        platform = skill / "references" / "platforms" / backend.value
        platform.mkdir(parents=True)
        (platform / "floor.md").write_text(f"# {backend.value} floor\n")
    return skill


@pytest.mark.parametrize("selected", tuple(ComputeBackend))
def test_skill_copy_excludes_only_foreign_platform_paths(
    tmp_path: Path,
    selected: ComputeBackend,
) -> None:
    source = _write_platform_skill(tmp_path / "skills")

    step = skill_copy(source, tmp_path / "workspace" / source.name, selected)

    assert step.excluded_relative_paths == frozenset(
        Path("references", "platforms", backend.value)
        for backend in ComputeBackend
        if backend is not selected
    )


def test_skill_copy_without_backend_keeps_every_platform(tmp_path: Path) -> None:
    source = _write_platform_skill(tmp_path / "skills")

    step = skill_copy(source, tmp_path / "workspace" / source.name, None)

    assert step.excluded_relative_paths == frozenset()


def test_fresh_plan_composes_all_selected_inputs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    input_dir = tmp_path / "input"
    evaluator = tmp_path / "evaluators" / "queue"
    skill = tmp_path / "skills" / "serving-systems"
    source = WorkspaceSource(
        name="library",
        repo="https://example.invalid/library.git",
        commit="0123456",
        dest="library",
    )

    plan = build_workspace_materialization_plan(
        root,
        existing=False,
        input_dir=input_dir,
        evaluator_source=evaluator,
        skill_sources=[skill],
        input_project_dir=input_dir,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
        compute_backend=ComputeBackend.CUDA,
        workspace_sources=(source,),
        extra_input_excludes=frozenset({"model"}),
    )

    assert plan == (
        materialization_source(source),
        ProjectTreeCopy(
            src=input_dir,
            dest=root,
            extra_excludes=frozenset({"model"}),
            reject_collisions=True,
        ),
        ProjectTreeCopy(
            src=evaluator,
            dest=root / "_evaluator" / "queue",
            respect_gitignore=True,
            require_absent=root / "_evaluator",
            require_absent_message=(
                "_evaluator is reserved for the manifest-declared evaluator source"
            ),
        ),
        skill_copy(skill, root / skill.name, ComputeBackend.CUDA),
        InputProjectMaterialization(project_dir=input_dir),
        ProjectTreeCopy(
            src=tmp_path / "profilers" / "nsys",
            dest=root / "nsys_profiler",
        ),
    )


def test_fresh_plan_stages_extra_profiler_support_dirs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    input_dir = tmp_path / "input"

    plan = build_workspace_materialization_plan(
        root,
        existing=False,
        input_dir=input_dir,
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=str(tmp_path / "profilers" / "rocprof"),
        profiler_support_name="rocprof_profiler",
        compute_backend=ComputeBackend.ROCM,
        profiler_support_extra=(
            (str(tmp_path / "profilers" / "_common"), "profilers_common"),
            (str(tmp_path / "profilers" / "torch"), "torch_profiler"),
        ),
    )

    assert plan == (
        ProjectTreeCopy(src=input_dir, dest=root),
        ProjectTreeCopy(
            src=tmp_path / "profilers" / "rocprof",
            dest=root / "rocprof_profiler",
        ),
        ProjectTreeCopy(
            src=tmp_path / "profilers" / "_common",
            dest=root / "profilers_common",
        ),
        ProjectTreeCopy(
            src=tmp_path / "profilers" / "torch",
            dest=root / "torch_profiler",
        ),
    )


def test_resume_plan_refreshes_skills_and_missing_profiler(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    skill = tmp_path / "skills" / "serving-systems"
    (root / skill.name).mkdir(parents=True)
    (root / ".claude" / "skills" / skill.name).mkdir(parents=True)

    plan = build_workspace_materialization_plan(
        root,
        existing=True,
        input_dir=tmp_path / "input",
        evaluator_source=tmp_path / "evaluator",
        skill_sources=[skill],
        input_project_dir=tmp_path / "input",
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
        compute_backend=ComputeBackend.CUDA,
    )

    assert plan == (
        skill_copy(skill, root / skill.name, ComputeBackend.CUDA),
        skill_copy(
            skill,
            root / ".claude" / "skills" / skill.name,
            ComputeBackend.CUDA,
        ),
        ProjectTreeCopy(
            src=tmp_path / "profilers" / "nsys",
            dest=root / "nsys_profiler",
        ),
    )


def test_resume_plan_skips_existing_profiler_support(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "nsys_profiler").mkdir(parents=True)

    plan = build_workspace_materialization_plan(
        root,
        existing=True,
        input_dir=tmp_path / "input",
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
        compute_backend=ComputeBackend.CUDA,
    )

    assert plan == ()
