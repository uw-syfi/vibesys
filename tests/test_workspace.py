"""Workspace setup-plan and copy-machinery tests.

The copy policies are declarative (``plan_setup`` returns CopySpec /
InputProjectSpec records), so most tests assert on the plan itself and
only the edge-case tests materialize files.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vibesys.run import CopySpec, InputProjectSpec, Workspace


def _make_workspace(root, *, isolated=False, excluded_dirs=None):
    return Workspace(
        root,
        run_environment=SimpleNamespace(isolated=isolated),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=root.parent,
        excluded_dirs=excluded_dirs if excluded_dirs is not None else {".git", "target"},
    )


def test_fresh_plan_with_seed_overlays_input_and_rejects_collisions(tmp_path):
    ws = _make_workspace(tmp_path / "ws")
    seed = tmp_path / "seed"
    input_dir = tmp_path / "input"
    evaluator = tmp_path / "evaluators" / "queue"
    skills = [tmp_path / "skills" / "serving-systems"]

    plan = ws.plan_setup(
        existing=False,
        seed=seed,
        input_dir=input_dir,
        evaluator_source=evaluator,
        skill_sources=skills,
        input_project_dir=input_dir,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
        extra_input_excludes=frozenset({"model"}),
    )

    assert plan == (
        CopySpec(src=seed, dest=ws.root, respect_gitignore=True),
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
        CopySpec(src=skills[0], dest=ws.root / "serving-systems"),
        InputProjectSpec(project_dir=input_dir),
        CopySpec(src=tmp_path / "profilers" / "nsys", dest=ws.root / "nsys_profiler"),
    )


def test_fresh_plan_without_seed_does_not_reject_collisions(tmp_path):
    ws = _make_workspace(tmp_path / "ws")
    input_dir = tmp_path / "input"

    plan = ws.plan_setup(
        existing=False,
        seed=None,
        input_dir=input_dir,
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=None,
        profiler_support_name=None,
    )

    assert plan == (CopySpec(src=input_dir, dest=ws.root),)


def test_resume_plan_only_refreshes_skills_and_missing_profiler(tmp_path):
    root = tmp_path / "ws"
    skill = tmp_path / "skills" / "serving-systems"
    # Skill targets that exist in the interrupted workspace get refreshed —
    # both the workspace-root copy and the per-CLI mirror dirs.
    (root / "serving-systems").mkdir(parents=True)
    (root / ".claude" / "skills" / "serving-systems").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=True,
        seed=tmp_path / "seed",
        input_dir=tmp_path / "input",
        evaluator_source=tmp_path / "evaluator",
        skill_sources=[skill],
        input_project_dir=tmp_path / "input",
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
    )

    # No seed/input/evaluator/input-project copies on resume.
    assert plan == (
        CopySpec(src=skill, dest=root / "serving-systems"),
        CopySpec(src=skill, dest=root / ".claude" / "skills" / "serving-systems"),
        CopySpec(src=tmp_path / "profilers" / "nsys", dest=root / "nsys_profiler"),
    )


def test_resume_plan_skips_profiler_already_present(tmp_path):
    root = tmp_path / "ws"
    (root / "nsys_profiler").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=True,
        seed=None,
        input_dir=tmp_path / "input",
        evaluator_source=None,
        skill_sources=[],
        input_project_dir=None,
        profiler_support_path=str(tmp_path / "profilers" / "nsys"),
        profiler_support_name="nsys_profiler",
    )

    assert plan == ()


def test_setup_rejects_preexisting_evaluator_dir(tmp_path):
    root = tmp_path / "ws"
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    (evaluator / "checker.py").write_text("pass\n")
    # The input bundle smuggled in its own _evaluator dir.
    (root / "_evaluator").mkdir(parents=True)

    ws = _make_workspace(root)
    plan = ws.plan_setup(
        existing=False,
        seed=None,
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


def test_setup_prunes_excluded_dirs_on_fresh_runs_only(tmp_path):
    root = tmp_path / "ws"
    (root / "target").mkdir(parents=True)
    (root / "target" / "stale.o").write_text("stale")

    ws = _make_workspace(root)
    ws.setup((), existing=True)
    assert (root / "target" / "stale.o").exists()

    ws.setup((), existing=False)
    assert not (root / "target").exists()


def test_copy_dir_replaces_external_symlinks_when_not_isolated(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    os.symlink(outside, src / "model")

    dst = tmp_path / "ws"
    _make_workspace(dst, isolated=False).copy_dir(src, dst)

    assert not (dst / "model").exists()
    assert (dst / "model.symlink_target").read_text() == str(outside.resolve())


def test_copy_dir_removes_external_symlinks_when_isolated(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    os.symlink(outside, src / "model")
    (src / "kept.py").write_text("pass\n")

    dst = tmp_path / "ws"
    _make_workspace(dst, isolated=True).copy_dir(src, dst)

    assert not (dst / "model").exists()
    assert not (dst / "model.symlink_target").exists()
    assert (dst / "kept.py").is_file()
