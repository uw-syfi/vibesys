"""Every image-building CI consumer restores and publishes shared installed layers."""

from __future__ import annotations

from pathlib import Path

import pytest
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
    assert "imagetools inspect" in bases["run"]
    assert "{{.Manifest.Digest}}" in bases["run"]
    assert "timeout --kill-after=5s 60s" in bases["run"]
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


def test_registry_failures_do_not_silently_trigger_cold_installs() -> None:
    script = (REPO / "scripts/ci_registry_image_cache.sh").read_text()
    assert "manifest unknown" in script
    assert 'cat "${log}" >&2\n            return 1' in script
    assert "timeout --kill-after=5s 180s docker pull" in script
    assert "timeout --kill-after=5s 180s docker push" in script
    assert "org.opencontainers.image.source" in script
