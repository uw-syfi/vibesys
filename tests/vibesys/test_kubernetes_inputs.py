"""Standalone Kubernetes inputs must validate without repository submodules."""

import importlib.util
import tomllib
from itertools import pairwise
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from vibesys.evaluators import PROJECT_ROOT_TOKEN
from vibesys.input_manifest import load_project_task
from vibesys.run.project import ProjectProvisioningSpec, provision_project
from vibesys.run.workspace import GitSourceSpec, Workspace
from vibesys.sandbox.run_environment import LocalEnvironment
from vs_project import Project

PROJECT_ROOT = Path(__file__).parents[2]
MICROSERVICE_ROOT = PROJECT_ROOT / "examples" / "microservices"
PACKAGE_ROOT = PROJECT_ROOT / "resources" / "evaluators" / "microservice"
TASK_DIRS = {
    "hotel-reservation": MICROSERVICE_ROOT / "hotel-correctness/.vibesys/tasks/kubernetes",
    "social-network": MICROSERVICE_ROOT / "social-network-kubernetes/.vibesys/tasks/kubernetes",
    "train-ticket": MICROSERVICE_ROOT / "train-ticket-kubernetes/.vibesys/tasks/kubernetes",
}
KUBERNETES_SCENARIOS = {
    "hotel-reservation": "867806e575e1f7fb24437ae969910ddb17a76121",
    "social-network": "867806e575e1f7fb24437ae969910ddb17a76121",
    "train-ticket": "350f62000e6658e0e543730580c599d8558253e7",
}


@pytest.mark.parametrize(
    ("scenario", "commit"),
    [
        (name, commit)
        for name, commit in KUBERNETES_SCENARIOS.items()
        if name != "hotel-reservation"
    ],
)
def test_kubernetes_input_uses_packaged_lifecycle_and_pinned_source(
    scenario: str, commit: str
) -> None:
    root = MICROSERVICE_ROOT / f"{scenario}-kubernetes"
    project = Project.open(root)
    bundle = load_project_task(project, project.select_task("kubernetes"))
    config = f"{PROJECT_ROOT_TOKEN}/.vibesys/tasks/kubernetes/runtime.yaml"

    assert bundle.evaluator_package_digest is not None
    assert bundle.manifest.workspace is not None
    assert bundle.manifest.workspace.sources[0].commit == commit
    assert bundle.benchmark_command[:2] == (
        "${PYTHON}",
        str(PACKAGE_ROOT / "kubernetes_runtime" / "cli.py"),
    )
    assert ("--config", config) in set(pairwise(bundle.benchmark_command))
    assert ("--candidate-dir", PROJECT_ROOT_TOKEN) in set(pairwise(bundle.benchmark_command))
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "primary_value"
    assert "--local --run-environment local --profiler none" in (root / "README.md").read_text()


@pytest.mark.parametrize("scenario", KUBERNETES_SCENARIOS)
def test_kubernetes_assets_are_namespace_scoped_and_build_candidate_images(scenario: str) -> None:
    directory = TASK_DIRS[scenario]
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
    project = Project.open(MICROSERVICE_ROOT / "train-ticket-kubernetes")
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
    reference = (
        MICROSERVICE_ROOT
        / "train-ticket"
        / ".vibesys"
        / "tasks"
        / "default"
        / "benchmark"
        / "workload.toml"
    )
    packaged = TASK_DIRS["train-ticket"] / "workload.toml"
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
    assert "${PROJECT_ROOT}/evaluator/run.py" in bundle.accuracy_command
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
        "evaluator/run.py",
        "evaluator/runtime.mod",
        "evaluator/internal/hotel/suite.go",
        "benchmark/workload.toml",
    ):
        assert (destination / path).read_bytes() == (source / path).read_bytes()
    assert not (destination / "vibesys.input.toml").exists()


def test_train_ticket_manifest_matches_generator_output() -> None:
    directory = TASK_DIRS["train-ticket"]
    spec = importlib.util.spec_from_file_location(
        "train_ticket_generate_manifest", directory / "generate_manifest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = yaml.safe_dump_all(module.objects(), sort_keys=False)
    assert (directory / "manifest.yaml").read_text(encoding="utf-8") == generated
