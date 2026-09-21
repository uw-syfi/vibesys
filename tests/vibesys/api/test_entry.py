"""Tests for the `vibesys.api` entry functions (config/validate/default request)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api import LoopKind
from vibesys.api.entry import default_request, validate
from vs_project import Project

if TYPE_CHECKING:
    from pathlib import Path

_EXAMPLE = "examples/model-serving/whisper-large-v3"


def test_default_request_builds_a_runnable_request(repo_root: Path) -> None:
    """`default_request` sources config and input bundle from the project on disk."""
    project = Project.open(repo_root / _EXAMPLE)

    request = default_request(project, LoopKind.AGENT)

    assert request.loop is LoopKind.AGENT
    assert request.project_root == project.root
    assert request.objective  # the example ships an objective
    # Every bundle path the example declares exists, so it validates clean.
    assert validate(request) == []


def test_validate_reports_missing_input_and_evaluator(repo_root: Path, tmp_path: Path) -> None:
    """`validate` collects a diagnostic per missing on-disk path, not just the first."""
    request = default_request(Project.open(repo_root / _EXAMPLE), LoopKind.AGENT)
    broken_bundle = request.input_bundle.model_copy(
        update={
            "root": tmp_path / "missing-root",
            "evaluator_path": tmp_path / "missing-root" / "evaluator.py",
        }
    )
    broken = request.model_copy(update={"input_bundle": broken_bundle})

    diagnostics = validate(broken)

    assert {d.code for d in diagnostics} == {"missing_input", "missing_evaluator"}
    assert all(d.stage == "input_validation" for d in diagnostics)
