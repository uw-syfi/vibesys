"""Every image-building CI consumer restores and publishes shared installed layers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from scripts.ci_registry_image_cache import registry_cache

if TYPE_CHECKING:
    from collections.abc import Sequence
import yaml

REPO = Path(__file__).resolve().parents[2]
JOBS = ("feature-tests", "real-tiers-minimal-container", "agent-image")
ACTION = "./.github/actions/docker-image-cache"
PROJECTS = (
    "libs/vs-agent/src/vs_agent/images/features",
    "libs/vs-runtime/src/vs_runtime/features",
    "libs/vs-sandbox/src/vs_sandbox/features",
)


@pytest.mark.regression
@pytest.mark.parametrize("job", JOBS)
def test_image_builds_share_install_layers_across_pr_and_queue_refs(job: str) -> None:
    document = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())
    config = document["jobs"][job]
    steps = config["steps"]
    restore = next(step for step in steps if step.get("id") == "docker-cache")
    assert restore["uses"] == ACTION
    assert restore["with"]["mode"] == "restore"
    key = restore["with"]["key"]
    assert "steps.image-bases.outputs.digest" in key
    assert "hashFiles(" in key
    assert all(f"'{project}/**'" in key for project in PROJECTS)
    assert "feature_dockerfile.py" in key
    assert all(value not in key for value in ("github.sha", "github.ref", "outputs.week"))
    if job == "feature-tests":
        assert "matrix.family" in key
    bases = next(step for step in steps if step.get("id") == "image-bases")
    assert "sha256sum scripts/ci_base_images.toml" in bases["run"]
    assert "imagetools inspect" not in bases["run"]
    attach = next(step for step in steps if "data_root.sh attach" in step.get("run", ""))
    snapshot = next(step for step in steps if "data_root.sh snapshot" in step.get("run", ""))
    save = next(step for step in steps if step.get("with", {}).get("mode") == "save")
    assert save["uses"] == ACTION
    assert save["with"]["key"] == "${{ steps.docker-cache.outputs.cache-primary-key }}"
    assert save["with"]["restore-keys"].strip() == restore["with"]["restore-keys"].strip()
    assert steps.index(bases) < steps.index(restore) < steps.index(attach)
    assert steps.index(attach) < steps.index(snapshot) < steps.index(save)
    for step in (snapshot, save):
        assert "github.event_name == 'push'" not in step["if"]
        assert "github.event_name != 'pull_request'" in step["if"]
        assert "head.repo.full_name == github.repository" in step["if"]
        assert "cache-hit != 'true'" in step["if"]
    assert config["permissions"] == {"contents": "read", "packages": "write"}


def test_snapshots_keep_tagged_images_and_the_entire_image_store() -> None:
    script = (REPO / "scripts/ci_docker_data_root.sh").read_text()
    assert '"containerd-snapshotter": false' in script
    snapshot = script[script.index("snapshot() {") :]
    assert "docker rmi" not in snapshot
    assert "docker image prune" not in snapshot
    assert (
        snapshot.index("stop_docker") < snapshot.index("sudo tar") < snapshot.index("start_docker")
    )


def test_minimal_container_supplies_cache_sources_through_the_validated_pytest_option() -> None:
    workflow = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["real-tiers-minimal-container"]["steps"]
    run = next(step["run"] for step in steps if step.get("name") == "Run the tier")
    assert '--image-cache-from "$image"' in run
    assert 'run_minimal_container_tests.sh "${cache_sources[@]}"' in run
    assert "vibesys-agent-build:*" in run
    conftest = (REPO / "tests/minimal_container/conftest.py").read_text()
    assert '"--image-cache-from",' in conftest
    assert 'request.config.getoption("--image-cache-from")' in conftest


@pytest.mark.parametrize("mode", ["restore", "save"])
@pytest.mark.parametrize("operation", ["login", "pull", "create", "cp", "build", "push", "tag"])
def test_registry_errors_warn_and_allow_the_required_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, operation: str
) -> None:
    archive = tmp_path / "docker-data.tar.zst"
    if mode == "save":
        archive.write_bytes(b"snapshot")
    env = {
        "CACHE_MODE": mode,
        "CACHE_PATH": str(archive),
        "CACHE_KEY": "key",
        "CACHE_RESTORE_KEYS": "fallback",
        "CACHE_TOKEN": "test-token",
        "GITHUB_ACTOR": "actor",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "RUNNER_TEMP": str(tmp_path),
    }
    registry = FakeRegistry(operation)
    hit = registry_cache(env, registry)
    failed = any(call[1] == operation for call in registry.calls)
    if failed:
        assert "::warning::" in capsys.readouterr().out
        assert hit is False
    if mode == "restore":
        assert (tmp_path / "outputs").read_text() == f"cache-hit={str(hit).lower()}\n"
        assert archive.exists() is hit
    else:
        assert archive.read_bytes() == b"snapshot"


class FakeRegistry:
    """A registry with explicit operation failures and an in-memory archive payload."""

    def __init__(self, fail: str) -> None:
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], *, input_text: str | None = None) -> str:
        del input_text
        self.calls.append(tuple(argv))
        assert argv[1] in {"login", "pull", "create", "cp", "rm", "build", "push", "tag"}
        if argv[1] == self.fail:
            if argv[1] == "cp":
                Path(argv[-1]).write_bytes(b"partial archive")
            message = "registry unavailable"
            raise OSError(message)
        if argv[1] == "cp":
            Path(argv[-1]).write_bytes(b"snapshot")
        return "container-id" if argv[1] == "create" else ""


def test_the_action_keeps_required_build_failures_outside_its_best_effort_boundary() -> None:
    script = (REPO / "scripts/ci_registry_image_cache.sh").read_text()
    assert "::warning::" in script
    assert 'timeout --kill-after=5s "${cache_seconds}s" python3' in script
    assert "cache_seconds=60" in script
    assert "cache_seconds=120" in script
    workflow = yaml.safe_load((REPO / ".github/workflows/test.yml").read_text())
    for job in JOBS:
        steps = workflow["jobs"][job]["steps"]
        attach = next(
            i for i, step in enumerate(steps) if "data_root.sh attach" in step.get("run", "")
        )
        snapshot = next(
            i for i, step in enumerate(steps) if "data_root.sh snapshot" in step.get("run", "")
        )
        for step in steps[attach + 1 : snapshot]:
            assert not step.get("continue-on-error", False)
