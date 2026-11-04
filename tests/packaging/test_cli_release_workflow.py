"""Static contracts for the vibesys-cli release workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cli-release.yml"


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_only_a_cli_version_tag_starts_the_release() -> None:
    # YAML 1.1 reads the key `on` as True.
    triggers = _workflow()[True]
    assert triggers == {"push": {"tags": ["cli-v*"]}}


def test_building_has_no_write_access_and_publishing_comes_after_it() -> None:
    jobs = _jobs()
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in jobs["build"]
    assert jobs["publish"]["needs"] == "build"
    assert jobs["publish"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }


def test_every_action_is_pinned_to_a_commit() -> None:
    uses = re.findall(r"^\s+uses: (\S+)", WORKFLOW.read_text(), flags=re.MULTILINE)
    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference), reference


def test_no_release_takes_the_repository_latest_marker() -> None:
    # The Python releases use GitHub's "latest"; a release created without --latest=false would
    # take it, so every `gh release create` must refuse it.
    text = WORKFLOW.read_text()
    creates = re.findall(r"gh release create .*(?:\\\n.*)*", text)
    assert len(creates) == 2
    assert all("--latest=false" in create for create in creates)


def test_release_is_built_by_the_script_the_tests_check() -> None:
    steps = [step for job in _jobs().values() for step in job["steps"]]
    runs = [step.get("run", "") for step in steps]
    assert any('./cli/build_release.sh "${TAG#cli-v}"' in run for run in runs)
    assert any("./cli/check.sh" in run for run in runs)
