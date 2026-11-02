"""The CI job that builds changed example Dockerfiles stays build-only, selected, and cached.

The selection itself (which directories a change picks, and that a Dockerfile neither built nor
listed fails validation) is held by repoctl's own tests and `tests/repoctl/cases.toml`. These checks
hold the wiring between that selection and the workflow: the job reads the collection repoctl
publishes, runs the build group and nothing else, and caches layers the way the other image jobs do.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
JOB = "example-dockerfiles"
GROUP = "example_dockerfile_builds"
COLLECTION = "dockerfile_contexts"


def _job() -> dict[str, Any]:
    document = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())
    return document["jobs"][JOB]


def _policy(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / ".repoctl" / name).read_text())


def _discovery() -> dict[str, Any]:
    return next(
        item
        for item in _policy("components.toml")["discoveries"]
        if item.get("class") == "dockerfile"
    )


def test_the_job_expands_the_collection_repoctl_publishes_one_leg_per_directory() -> None:
    job = _job()
    assert job["strategy"]["matrix"]["context"] == (
        "${{ fromJSON(needs.changes.outputs." + COLLECTION + ") }}"
    )
    assert COLLECTION in {item["name"] for item in _policy("components.toml")["collections"]}
    changes = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())["jobs"]["changes"]
    assert COLLECTION in changes["outputs"]
    assert _discovery()["selected_jobs"][0] in changes["outputs"]


def test_the_job_runs_on_pull_requests_and_in_the_queue_but_skips_when_nothing_is_selected() -> (
    None
):
    condition = " ".join(str(_job()["if"]).split())
    assert "github.event_name" not in condition
    assert f"outputs.{COLLECTION} != '[]'" in condition


def test_the_group_only_builds_and_the_job_needs_no_secret_or_accelerator() -> None:
    group = next(g for g in _policy("checks.toml")["check_groups"] if g["name"] == GROUP)
    assert group["trigger_job"] == _discovery()["selected_jobs"][0]
    assert group["collection"] == COLLECTION
    assert all(command[:2] == ["docker", "build"] for command in group["package_commands"])
    job = _job()
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] <= 10
    assert "secrets." not in yaml.safe_dump(job)
    assert "gpu" not in yaml.safe_dump(job).lower()


def test_the_layer_cache_is_named_by_the_dockerfile_and_saved_after_the_build() -> None:
    steps = _job()["steps"]
    restore = next(step for step in steps if step.get("id") == "docker-cache")
    assert restore["with"]["mode"] == "restore"
    assert "steps.dockerfile.outputs.digest" in restore["with"]["key"]
    assert all(
        value not in restore["with"]["key"] for value in ("github.sha", "github.ref", "run_id")
    )
    attach = next(step for step in steps if "data_root.sh attach" in step.get("run", ""))
    build = next(step for step in steps if f"--group {GROUP}" in step.get("run", ""))
    snapshot = next(step for step in steps if "data_root.sh snapshot" in step.get("run", ""))
    save = next(step for step in steps if step.get("with", {}).get("mode") == "save")
    assert steps.index(restore) < steps.index(attach) < steps.index(build)
    assert steps.index(build) < steps.index(snapshot) < steps.index(save)
    for step in (snapshot, save):
        assert "cache-hit != 'true'" in step["if"]
        assert "head.repo.full_name == github.repository" in step["if"]
    assert _job()["permissions"] == {"contents": "read", "packages": "write"}
