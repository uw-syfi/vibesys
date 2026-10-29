"""Product composition tests for VibeSys's bundled evaluator collection."""

from __future__ import annotations

import pytest

from vibesys.config import BUNDLED_RESOURCES
from vs_runtime.api.infrastructure import (
    EvaluatorPackageRequirement,
    ResolvedEvaluatorPackage,
    load_evaluator_package,
    resolve_evaluator_package,
)


def _resolve_bundled_package(name: str) -> ResolvedEvaluatorPackage:
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    assert packages_root is not None
    return resolve_evaluator_package(
        packages_root,
        EvaluatorPackageRequirement(name=name, version="0.1.0"),
    )


def test_framework_resolver_finds_bundled_queue_package() -> None:
    package = _resolve_bundled_package("vibesys-evaluator-queue")

    assert package.root.name == "queue"
    assert package.command("vibesys-queue")[:3] == ("go", "-C", str(package.root))


@pytest.mark.parametrize(
    ("name", "entrypoints"),
    [
        ("vibesys-evaluator-queue", {"vibesys-queue"}),
        (
            "vibesys-evaluator-microservice",
            {"kubernetes-runtime", "otelcapture", "otelinject", "python", "servicebench"},
        ),
        (
            "vibesys-evaluator-request-factory",
            {
                "request-factory-adapter",
                "request-factory-engine",
                "request-factory-fixed-text-v1",
            },
        ),
    ],
)
def test_bundled_evaluator_package_metadata(name: str, entrypoints: set[str]) -> None:
    package = _resolve_bundled_package(name)

    assert set(package.metadata.entrypoints) == entrypoints
    assert len(package.digest) == len("sha256:") + 64


def test_bundled_evaluator_packages_declare_only_required_toolchains() -> None:
    queue = _resolve_bundled_package("vibesys-evaluator-queue")
    microservice = _resolve_bundled_package("vibesys-evaluator-microservice")

    assert queue.metadata.toolchains == ("go", "rust")
    assert microservice.metadata.toolchains == ("go",)


def test_every_bundled_go_run_entrypoint_declares_one_complete_package_module() -> None:
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    assert packages_root is not None
    declared: dict[str, set[str]] = {}
    for metadata in sorted(packages_root.glob("*/vibesys.evaluator.toml")):
        package = load_evaluator_package(metadata.parent)
        go_entrypoints = {
            name
            for name, command in package.metadata.entrypoints.items()
            if "go" in command and "run" in command
        }
        if not go_entrypoints:
            continue
        assert package.go_module_root is not None
        assert (package.go_module_root / "go.mod").is_file()
        assert (package.go_module_root / "go.sum").is_file()
        declared[package.root.name] = go_entrypoints

    assert declared == {
        "microservice": {"otelcapture", "otelinject", "servicebench"},
        "ordered-map": {"vibesys-ordered-map"},
        "priority-queue": {"vibesys-priority-queue"},
        "queue": {"vibesys-queue"},
        "stack": {"vibesys-stack"},
        "unordered-map": {"vibesys-unordered-map"},
    }


def test_bundled_request_factory_package_pins_cargo_git_tool() -> None:
    package = _resolve_bundled_package("vibesys-evaluator-request-factory")

    tool = package.metadata.tools["request-factory"]
    assert tool.rev == "89dce4a64ae12e7084fbe464ddda882fa0a4c482"
    assert tool.package == "req-frontend"
    assert tool.bins == ("session_runner",)
