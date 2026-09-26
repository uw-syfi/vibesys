"""Kubernetes inputs must validate and match their packaged evaluator.

The Train Ticket task lives in an external repository example. Its tests skip
locally until ``scripts/example_repositories.py`` has fetched it, and fail in CI
(``VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS=1``).
"""

import importlib.util
import tomllib
from itertools import pairwise
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from tests.support.example_registry import require_external_repo_checkout

from vibesys.evaluators import PROJECT_ROOT_TOKEN
from vibesys.evaluators.input_manifest import InputBundle, load_project_task
from vibesys.run.project import ProjectProvisioningSpec, provision_project
from vibesys.run.workspace import GitSourceSpec, Workspace
from vibesys.sandbox.run_environment import LocalEnvironment
from vs_project.api import Project

PROJECT_ROOT = Path(__file__).parents[2]
MICROSERVICE_ROOT = PROJECT_ROOT / "examples" / "microservices"
PACKAGE_ROOT = PROJECT_ROOT / "resources" / "evaluators" / "microservice"
TRAIN_TICKET_EXAMPLE = "examples/microservices/repositories/train-ticket"
TRAIN_TICKET_ROOT = PROJECT_ROOT / TRAIN_TICKET_EXAMPLE
TASK_DIRS = {
    "hotel-reservation": MICROSERVICE_ROOT / "hotel-correctness/.vibesys/tasks/kubernetes",
    "social-network": MICROSERVICE_ROOT / "social-network-kubernetes/.vibesys/tasks/kubernetes",
    "train-ticket": TRAIN_TICKET_ROOT / ".vibesys/tasks/kubernetes",
}
# Scenarios that clone a pinned upstream commit as a workspace source. Train
# Ticket does not: its fork is the candidate, so there is nothing to pin.
KUBERNETES_SCENARIOS = {
    "hotel-reservation": "867806e575e1f7fb24437ae969910ddb17a76121",
    "social-network": "867806e575e1f7fb24437ae969910ddb17a76121",
}


def _task_dir(scenario: str) -> Path:
    if scenario == "train-ticket":
        require_external_repo_checkout(TRAIN_TICKET_EXAMPLE)
    return TASK_DIRS[scenario]


def _assert_packaged_lifecycle(bundle: InputBundle, readme: Path) -> None:
    config = f"{PROJECT_ROOT_TOKEN}/.vibesys/tasks/kubernetes/runtime.yaml"
    assert bundle.evaluator_package_digest is not None
    assert bundle.benchmark_command[:2] == (
        "${PYTHON}",
        str(PACKAGE_ROOT / "kubernetes_runtime" / "cli.py"),
    )
    assert ("--config", config) in set(pairwise(bundle.benchmark_command))
    assert ("--candidate-dir", PROJECT_ROOT_TOKEN) in set(pairwise(bundle.benchmark_command))
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "primary_value"
    assert "--local --run-environment local --profiler none" in readme.read_text()


def test_social_kubernetes_input_uses_packaged_lifecycle_and_pinned_source() -> None:
    root = MICROSERVICE_ROOT / "social-network-kubernetes"
    project = Project.open(root)
    bundle = load_project_task(project, project.select_task("kubernetes"))

    _assert_packaged_lifecycle(bundle, root / "README.md")
    assert bundle.manifest.workspace is not None
    assert bundle.manifest.workspace.sources[0].commit == KUBERNETES_SCENARIOS["social-network"]


def test_train_ticket_kubernetes_input_uses_packaged_lifecycle_and_repository_source() -> None:
    require_external_repo_checkout(TRAIN_TICKET_EXAMPLE)
    project = Project.open(TRAIN_TICKET_ROOT)
    bundle = load_project_task(project, project.select_task("kubernetes"))

    _assert_packaged_lifecycle(bundle, TRAIN_TICKET_ROOT / ".vibesys/README.md")
    # The fork is the candidate: nothing is cloned into the workspace, and every
    # image builds from the candidate root.
    assert bundle.workspace_sources == ()
    config = yaml.safe_load((TASK_DIRS["train-ticket"] / "runtime.yaml").read_text())
    assert {build["context"] for build in config["image_builds"]} == {"."}


@pytest.mark.parametrize("scenario", TASK_DIRS)
def test_kubernetes_assets_are_namespace_scoped_and_build_candidate_images(scenario: str) -> None:
    directory = _task_dir(scenario)
    config = yaml.safe_load((directory / "runtime.yaml").read_text())
    resources = [
        resource
        for filename in config["manifests"]
        for resource in yaml.safe_load_all((directory / filename).read_text())
        if resource is not None
    ]
    assert resources
    assert all(resource["kind"] in {"Deployment", "Service", "ConfigMap"} for resource in resources)
    assert all("namespace" not in resource["metadata"] for resource in resources)
    deployments = {
        resource["metadata"]["name"]: resource
        for resource in resources
        if resource["kind"] == "Deployment"
    }
    assert config["image_builds"]
    for override in config["image_overrides"]:
        deployment = deployments[override["resource"].removeprefix("deployment/")]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        assert override["container"] in {container["name"] for container in containers}
        assert override["image"].startswith("${IMAGE:")
    for forward in config["forwards"]:
        assert forward["resource"].startswith("service/")


def test_train_ticket_accuracy_uses_all_named_service_forwards() -> None:
    require_external_repo_checkout(TRAIN_TICKET_EXAMPLE)
    project = Project.open(TRAIN_TICKET_ROOT)
    bundle = load_project_task(project, project.select_task("kubernetes"))
    assert ("--mode", "accuracy") in set(pairwise(bundle.accuracy_command))
    for target in ("config", "station", "train", "travel", "route", "price"):
        assert f"{target}=${{ENDPOINT:{target}}}" in bundle.accuracy_command


def test_social_accuracy_runs_semantically_validated_light_profile() -> None:
    project = Project.open(MICROSERVICE_ROOT / "social-network-kubernetes")
    bundle = load_project_task(project, project.select_task("kubernetes"))
    pairs = set(pairwise(bundle.accuracy_command))
    assert ("--profile", "light") in pairs
    assert ("--seed", "random") in pairs
    assert ("--fixture-seed", "random") in pairs


def test_train_ticket_workload_preserves_canonical_semantic_mix() -> None:
    packaged = _task_dir("train-ticket") / "workload.toml"
    reference = TRAIN_TICKET_ROOT / ".vibesys/tasks/default/benchmark/workload.toml"
    expected = tomllib.loads(reference.read_text())
    actual = tomllib.loads(packaged.read_text())
    for field in ("application", "load", "operations", "objective", "constraints"):
        assert actual[field] == expected[field]


def test_hotel_crash_stops_volatile_cache_but_preserves_mongodb() -> None:
    path = TASK_DIRS["hotel-reservation"] / "runtime.yaml"
    config = yaml.safe_load(path.read_text())
    restarted = {deployment["name"] for deployment in config["restart_deployments"]}
    assert restarted == {
        "frontend",
        "geo",
        "profile",
        "rate",
        "recommendation",
        "reservation",
        "search",
        "user",
        "memcached-reserve",
    }
    assert not any(name.startswith("mongodb-") for name in restarted)


@pytest.mark.parametrize("task_name", ["compose", "kubernetes"])
def test_hotel_tasks_share_packaged_go_oracle(task_name: str) -> None:

    project = Project.open(MICROSERVICE_ROOT / "hotel-correctness")
    assert {task.name.value for task in project.discover_tasks()} == {"compose", "kubernetes"}
    bundle = load_project_task(project, project.select_task(task_name))
    assert bundle.evaluator_path is None
    assert bundle.evaluator_package_root == PACKAGE_ROOT
    assert bundle.evaluator_package_digest
    assert "${PROJECT_ROOT}/.vibesys/tasks/compose/evaluator/run.py" in bundle.accuracy_command
    assert ("--package-root", str(PACKAGE_ROOT)) in set(pairwise(bundle.accuracy_command))
    if task_name == "kubernetes":
        assert "${KUBERNETES_STOP_COMMAND_JSON}" in bundle.accuracy_command
        assert "${KUBERNETES_START_COMMAND_JSON}" in bundle.accuracy_command
    assert bundle.manifest.workspace is not None
    assert bundle.manifest.workspace.sources[0].commit == KUBERNETES_SCENARIOS["hotel-reservation"]


def test_hotel_native_task_materializes_shared_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    source = MICROSERVICE_ROOT / "hotel-correctness"
    project = Project.open(source)
    bundle = load_project_task(project, project.select_task("kubernetes"))
    destination = tmp_path / "project"
    workspace = Workspace(
        destination,
        run_environment=LocalEnvironment(),
        backend=MagicMock(),
        log=MagicMock(),
        project_root=tmp_path,
    )
    real_setup = workspace.setup

    def setup_without_network(steps: tuple, *, existing: bool) -> None:
        # Source checkout is orthogonal; exercise actual bundle copying and task preservation.
        real_setup(
            tuple(step for step in steps if not isinstance(step, GitSourceSpec)), existing=existing
        )

    monkeypatch.setattr(workspace, "setup", setup_without_network)
    assert bundle.manifest.workspace is not None
    provision_project(
        source,
        destination,
        spec=ProjectProvisioningSpec(
            workspace=workspace,
            workspace_sources=bundle.manifest.workspace.sources,
            task_name="kubernetes",
            input_project_dir=source,
        ),
    )
    copied = Project.open(destination)
    resolved = load_project_task(copied, copied.select_task("kubernetes"))
    assert resolved.accuracy_command == bundle.accuracy_command
    for path in (
        ".vibesys/tasks/compose/evaluator/run.py",
        ".vibesys/tasks/compose/evaluator/runtime.mod",
        ".vibesys/tasks/compose/evaluator/internal/hotel/suite.go",
        ".vibesys/tasks/compose/benchmark/workload.toml",
    ):
        assert (destination / path).read_bytes() == (source / path).read_bytes()
    assert not (destination / "vibesys.input.toml").exists()


def test_train_ticket_manifest_matches_generator_output() -> None:
    directory = _task_dir("train-ticket")
    spec = importlib.util.spec_from_file_location(
        "train_ticket_generate_manifest", directory / "generate_manifest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = yaml.safe_dump_all(module.objects(), sort_keys=False)
    assert (directory / "manifest.yaml").read_text(encoding="utf-8") == generated
