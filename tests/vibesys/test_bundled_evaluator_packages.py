"""Product composition tests for VibeSys's bundled evaluator collection."""

from __future__ import annotations

import pytest

from vibesys.config import BUNDLED_RESOURCES
from vs_runtime.api.infrastructure import (
    EvaluatorPackageRequirement,
    ResolvedEvaluatorPackage,
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


def test_bundled_request_factory_package_pins_cargo_git_tool() -> None:
    package = _resolve_bundled_package("vibesys-evaluator-request-factory")

    tool = package.metadata.tools["request-factory"]
    assert tool.rev == "89dce4a64ae12e7084fbe464ddda882fa0a4c482"
    assert tool.package == "req-frontend"
    assert tool.bins == ("session_runner",)
