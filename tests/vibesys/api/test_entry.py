"""Structural validation of canonical descriptor-backed run requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.api import Config, OrchestrationDescriptor, ResumeRef, RunRequest
from vibesys.api.request import load_input_bundle
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path

_EXAMPLE = "examples/model-serving/whisper-large-v3"


def _request(root: Path) -> RunRequest:
    bundle = load_input_bundle(root)
    return RunRequest(
        project_root=root,
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=bundle,
        objective=bundle.objective,
    )


def test_canonical_request_builds_a_runnable_request(repo_root: Path) -> None:
    project = Project.open(repo_root / _EXAMPLE)
    request = _request(project.root)

    assert request.orchestration.id == "multi-agent"
    assert request.project_root == project.root
    assert request.objective


def test_run_request_owns_fresh_and_resumed_identity(repo_root: Path) -> None:
    request = _request(Project.open(repo_root / _EXAMPLE).root)

    with pytest.raises(ValueError, match="exp_name must be set"):
        _ = request.resolved_run_id

    fresh = request.model_copy(update={"exp_name": "fresh-run"})
    resumed = fresh.model_copy(update={"resume": ResumeRef(run_id="prior-run")})

    assert fresh.resolved_run_id == "fresh-run"
    assert resumed.resolved_run_id == "prior-run"
