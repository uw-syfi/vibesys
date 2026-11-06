"""Static contracts for the vibesys-evaluator release workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import evaluator_wheels
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
        "sdk/vibesys-evaluator/go/v${{ needs.verify.outputs.go_version }}"
    )
    script = next(step["run"] for step in _steps() if step.get("id") == "version")
    assert "--go-module-version" in script


def test_the_release_runs_the_api_check_that_pull_requests_run() -> None:
    runs = [step.get("run", "") for step in _jobs()["verify"]["steps"]]
    assert "uv run --frozen python scripts/check_vibesys_evaluator_api.py" in runs


def test_the_image_installs_the_verified_linux_wheel_and_runs_no_go() -> None:
    build = next(s["run"] for s in _jobs()["verify"]["steps"] if s.get("id") == "image")
    assert '--build-context wheels="$RUNNER_TEMP/image-wheel"' in build
    assert 'cp "$LINUX_WHEEL" "$RUNNER_TEMP/image-wheel/"' in build
    # The binaries in the image are the wheel's, stamped by the wheel build.
    assert "VIBESYS_EVALUATOR_VERSION" not in build
    linux = next(s for s in _jobs()["verify"]["steps"] if s.get("id") == "linux_wheel")
    assert "manylinux_2_17_x86_64" in linux["run"]
    assert 'test "$#" -eq 1' in linux["run"]


def test_the_release_builds_every_platform_wheel_and_verifies_it_before_anything_is_published() -> (
    None
):
    steps = _jobs()["verify"]["steps"]
    names = [step.get("name", "") for step in steps]
    build = steps[names.index("Build the wheels")]["run"]
    assert "packaging/evaluator_wheels.py build" in build
    assert '--go-version "$GO_VERSION"' in build
    assert "--wheel-dir dist/evaluator --evaluator-wheels" in build
    assert 'packaging/evaluator_wheels.py verify dist/evaluator --version "$VERSION"' in build
    # Go is set up before the wheels are built (they cross-compile the binaries).
    assert names.index("Set up Go") < names.index("Build the wheels")
    # The wheel is installed with no Go toolchain, and a main() sample runs through its core.
    install = steps[names.index("Install the wheel in a clean environment")]["run"]
    # The clean PATH links the system commands but the Go toolchain's (the runner has go in
    # /usr/bin), and the step refuses to run if go is still reachable on it.
    assert 'clean_path="$root/clean-bin"' in install
    assert "go | gofmt | go[0-9]*) continue" in install
    assert 'PATH="$clean_path" command -v go' in install
    assert "evaluator_wheel_smoke.py" in install
    assert "benchmark --vs-output" in install
    # musl: the static Linux binaries run on Alpine.
    musl = "\n".join(s.get("run", "") for s in steps if "musl" in s.get("name", ""))
    assert "alpine@sha256:" in musl


def test_pypi_gets_the_platform_wheels_and_the_sdist_and_never_overwrites() -> None:
    steps = _jobs()["pypi"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)
    assert "-eq 5" in script
    assert "vibesys_evaluator-$VERSION.tar.gz" in script
    assert "*-any.whl" in script  # no universal wheel
    for platform in (
        "manylinux_2_17_x86_64",
        "manylinux_2_17_aarch64",
        "macosx_12_0_x86_64",
        "macosx_12_0_arm64",
    ):
        assert platform in script
    # A file PyPI already holds with other contents stops the release; the same one is skipped.
    assert "not the verified" in script
    publish = next(s for s in steps if s.get("uses", "").startswith("pypa/"))
    assert publish["if"] == "steps.pending.outputs.pending != '0'"
    assert "integrity/vibesys-evaluator" in script  # every file's attestation is checked


def test_the_wheel_matrix_the_workflow_checks_is_the_one_the_builder_makes() -> None:
    script = "\n".join(step.get("run", "") for step in _jobs()["pypi"]["steps"])
    for platform in evaluator_wheels.PLATFORMS:
        first = platform.tag.split(".")[0]
        assert first in script, platform


def test_python_checks_leave_no_bytecode_among_the_fixtures() -> None:
    assert _jobs()["verify"]["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_the_attestation_is_pushed_with_a_docker_login_that_is_removed_after() -> None:
    """The attest action reads Docker's credentials; skopeo's authfile is not enough (rc1)."""
    steps = _jobs()["image"]["steps"]
    names = [step.get("name", "") for step in steps]
    login = names.index("Log in to the registry for the attestation")
    attest = next(i for i, s in enumerate(steps) if "attest" in s.get("uses", ""))
    logout = names.index("Log out of the registry")
    assert login < attest < logout
    assert "docker login ghcr.io" in steps[login]["run"]
    assert "--password-stdin" in steps[login]["run"]
    assert steps[logout]["if"] == "always()"
    assert steps[logout]["run"] == "docker logout ghcr.io"


def test_a_missing_go_tag_is_not_read_as_an_existing_one() -> None:
    """A 404 body on stdout once read as the tag's commit and failed rc2 (exact ref, no ``|| true``)."""
    tag = next(s for s in _jobs()["go-tag"]["steps"] if s.get("name") == "Tag the Go module")
    script = tag["run"]
    assert "git/matching-refs/tags/$GO_TAG" in script
    assert 'select(.ref == \\"refs/tags/$GO_TAG\\")' in script
    assert "|| true" not in script
    assert "git/ref/tags/" not in script


def test_every_domain_image_is_built_smoked_pushed_and_recorded() -> None:
    """One image per domain, from the Dockerfile's target of that name (sdk/vibesys-evaluator)."""
    domains = _workflow()["env"]["DOMAINS"].split()
    assert domains == ["microservices", "llm-serving"]
    assert _jobs()["image"]["strategy"]["matrix"]["domain"] == domains
    dockerfile = (REPO_ROOT / "sdk/vibesys-evaluator/image/Dockerfile").read_text()
    targets = re.findall(r"^FROM \S+ AS (\S+)$", dockerfile, flags=re.MULTILINE)
    assert set(domains) <= set(targets)
    build = next(s["run"] for s in _jobs()["verify"]["steps"] if s.get("id") == "image")
    assert '--target "$domain"' in build
    assert "for domain in $DOMAINS" in build
    outputs = _jobs()["verify"]["outputs"]
    for domain in domains:
        key = f"digest_{domain.replace('-', '_')}"
        assert outputs[key] == f"${{{{ steps.image.outputs.{key} }}}}"
    notes = next(
        s["run"] for s in _jobs()["go-tag"]["steps"] if s.get("name", "").startswith("Record")
    )
    assert "for domain in $DOMAINS" in notes
