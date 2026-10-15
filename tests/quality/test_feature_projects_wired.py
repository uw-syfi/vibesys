"""Every Dev Container Feature project in the repository is checked and tested in CI.

A Feature project is an owner's ``features/`` folder holding ``src/<id>/`` and
``test/<id>/``. Owners pass their projects to the image mechanism as data, so
nothing in the build discovers a new one; a new project that is not added to CI
would ship Features that ``devcontainer features test`` never ran and that no
change to them would ever rebuild. This test derives the projects from the tree
and requires each to be well formed, selected by the ``agent_image`` component,
tested by the ``feature-tests`` job, and part of both Docker cache keys.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from vs_sandbox.api import feature_project_problems

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
COMPONENTS = REPO_ROOT / ".repoctl" / "components.toml"
TEST_SCRIPT = "libs/vs-sandbox/src/vs_sandbox/devcontainer-cli/test_features.sh"

PROJECTS = sorted(
    {
        metadata.parents[2].relative_to(REPO_ROOT).as_posix()
        for metadata in REPO_ROOT.glob("libs/*/src/*/**/features/src/*/devcontainer-feature.json")
    }
)


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _cache_key(job: str) -> str:
    step = next(s for s in _jobs()[job]["steps"] if s.get("id") == "docker-cache")
    return step["with"]["key"]


def _tested_projects() -> set[str]:
    runs = [step.get("run", "") for step in _jobs()["feature-tests"]["steps"]]
    argv = shlex.split(next(run for run in runs if TEST_SCRIPT in run))
    return set(argv[argv.index(TEST_SCRIPT) + 2 :])


def _agent_image_roots() -> list[str]:
    components = tomllib.loads(COMPONENTS.read_text(encoding="utf-8"))["components"]
    return next(c for c in components if c["id"] == "agent_image")["roots"]


def test_every_owner_project_is_found() -> None:
    assert set(PROJECTS) >= {
        "libs/vs-agent/src/vs_agent/images/features",
        "libs/vs-runtime/src/vs_runtime/features",
        "libs/vs-sandbox/src/vs_sandbox/features",
    }


@pytest.mark.parametrize("project", PROJECTS)
def test_the_project_is_well_formed(project: str) -> None:
    assert feature_project_problems(REPO_ROOT / project) == []


@pytest.mark.parametrize("project", PROJECTS)
def test_the_feature_tests_job_runs_the_project(project: str) -> None:
    assert project in _tested_projects(), (
        f"{project} is a Feature project; pass it to {TEST_SCRIPT} in the "
        f"feature-tests job of {WORKFLOW.relative_to(REPO_ROOT)}"
    )


@pytest.mark.parametrize("job", ["feature-tests", "agent-image"])
@pytest.mark.parametrize("project", PROJECTS)
def test_the_docker_cache_key_covers_the_project(project: str, job: str) -> None:
    assert f"'{project}/**'" in _cache_key(job), (
        f"add '{project}/**' to the hashFiles of the {job} job's Docker cache key"
    )


@pytest.mark.parametrize("project", PROJECTS)
def test_a_change_to_the_project_selects_the_image_jobs(project: str) -> None:
    roots = _agent_image_roots()
    assert any(project == root or project.startswith(f"{root}/") for root in roots), (
        f"{project} is under no root of the agent_image component in "
        f"{COMPONENTS.relative_to(REPO_ROOT)}; add it to that component's roots"
    )
