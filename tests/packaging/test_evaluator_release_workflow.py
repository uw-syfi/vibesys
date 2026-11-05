"""Static contracts for the vibesys-evaluator release workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "evaluator-release.yml"


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps() -> list[dict[str, Any]]:
    return [step for job in _jobs().values() for step in job["steps"]]


def _uses(path: Path) -> list[str]:
    return re.findall(r"^\s+(?:- )?uses: (\S+)", path.read_text(), flags=re.MULTILINE)


def test_only_a_published_release_starts_it_and_only_an_evaluator_tag_runs_it() -> None:
    # YAML 1.1 reads the key `on` as True.
    assert _workflow()[True] == {"release": {"types": ["published"]}}
    jobs = _jobs()
    assert jobs["verify"]["if"] == "startsWith(github.event.release.tag_name, 'evaluator-')"
    # Every other job runs only after verify, so a skipped verify skips the whole release.
    for name, job in jobs.items():
        if name != "verify":
            needs = job["needs"]
            assert "verify" in ([needs] if isinstance(needs, str) else needs), name


def test_the_tag_must_name_the_package_version_and_a_commit_on_main() -> None:
    script = next(step["run"] for step in _steps() if step.get("id") == "version")
    assert '--source-root "$PACKAGE_ROOT/python" --tag-prefix refs/tags/evaluator-v' in script
    assert '--tag "refs/tags/$TAG"' in script
    assert 'git merge-base --is-ancestor "$release_commit" origin/main' in script
    assert _workflow()["env"]["PACKAGE_ROOT"] == "sdk/vibesys-evaluator"


def test_each_privilege_is_held_by_exactly_one_job() -> None:
    jobs = _jobs()
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in jobs["verify"]
    assert jobs["pypi"]["permissions"] == {"actions": "read", "id-token": "write"}
    assert jobs["image"]["permissions"] == {
        "actions": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["go-tag"]["permissions"] == {"contents": "write"}
    # Only the pypi job runs in the environment the Trusted Publisher names, so the image job's
    # OIDC token (for its attestation) cannot publish to PyPI.
    assert jobs["pypi"]["environment"] == "pypi-evaluator"
    assert [name for name, job in jobs.items() if "environment" in job] == ["pypi"]


def test_publishing_comes_after_verification_and_pypi_comes_first() -> None:
    jobs = _jobs()
    assert jobs["pypi"]["needs"] == "verify"
    assert jobs["image"]["needs"] == ["verify", "pypi"]
    assert jobs["go-tag"]["needs"] == ["verify", "image"]


def test_a_started_release_is_serialized_and_never_cancelled() -> None:
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "'evaluator-release'" in concurrency["group"]


def test_pypi_publishes_the_verified_wheel_by_trusted_publishing() -> None:
    pypi_steps = _jobs()["pypi"]["steps"]
    assert all("run" not in step or "build" not in step["run"] for step in pypi_steps)
    download = next(s for s in pypi_steps if s.get("uses", "").startswith("actions/download-"))
    assert download["with"]["name"] == "evaluator-dist"
    publish = next(s for s in pypi_steps if s.get("uses", "").startswith("pypa/"))
    assert publish["with"] == {"packages-dir": "dist/evaluator", "attestations": True}
    # The same pinned publisher as the vibesys wheel.
    pypi_publish = [u for u in _uses(WORKFLOWS / "publish.yml") if u.startswith("pypa/")]
    assert publish["uses"] in pypi_publish


def test_every_action_is_pinned_to_a_commit_and_no_dependency_cache_is_used() -> None:
    uses = _uses(WORKFLOW)
    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", reference), reference
    for step in _steps():
        if step.get("uses", "").startswith("astral-sh/setup-uv@"):
            assert step["with"] == {"version": "0.9.24", "enable-cache": False}
        if step.get("uses", "").startswith("actions/setup-go@"):
            assert step["with"]["cache"] is False


def test_the_image_pushed_is_the_image_verified() -> None:
    verify = "\n".join(step.get("run", "") for step in _jobs()["verify"]["steps"])
    push = "\n".join(step.get("run", "") for step in _jobs()["image"]["steps"])
    assert "docker buildx build" in verify
    assert "docker build" not in push
    assert "docker buildx" not in push
    assert 'skopeo copy --authfile "$authfile" --preserve-digests' in push
    assert 'test "$pushed_digest" = "$VERIFIED_DIGEST"' in push
    attest = next(s for s in _jobs()["image"]["steps"] if "attest" in s.get("uses", ""))
    assert attest["with"]["push-to-registry"] is True
    assert attest["with"]["subject-digest"] == "${{ steps.push.outputs.digest }}"


def test_no_release_edit_takes_the_repository_latest_marker() -> None:
    # The vibesys Python releases hold GitHub's "latest"; see docs/contributing/publishing.md.
    text = WORKFLOW.read_text()
    assert "gh release create" not in text
    edits = re.findall(r"gh release edit .*", text)
    assert edits
    assert all("--latest=false" in edit for edit in edits)
    verify = "\n".join(step.get("run", "") for step in _jobs()["verify"]["steps"])
    assert "releases/latest" in verify


def test_the_go_module_tag_is_the_semver_of_the_release() -> None:
    job = _jobs()["go-tag"]
    assert job["env"]["GO_TAG"] == (
        "sdk/vibesys-evaluator/loadgen/v${{ needs.verify.outputs.go_version }}"
    )
    script = next(step["run"] for step in _steps() if step.get("id") == "version")
    assert "--go-module-version" in script
