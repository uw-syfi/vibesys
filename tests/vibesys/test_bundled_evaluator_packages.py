"""Product composition tests for VibeSys's bundled evaluator collection."""

from __future__ import annotations

import pytest

from vibesys.evaluators import EvaluatorPackageRequirement, resolve_evaluator_package


def test_framework_resolver_finds_bundled_queue_package() -> None:
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-queue", version="0.1.0")
    )

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
    package = resolve_evaluator_package(EvaluatorPackageRequirement(name=name, version="0.1.0"))

    assert set(package.metadata.entrypoints) == entrypoints
    assert len(package.digest) == len("sha256:") + 64


def test_bundled_evaluator_packages_declare_only_required_toolchains() -> None:
    queue = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-queue", version="0.1.0")
    )
    microservice = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-microservice", version="0.1.0")
    )

    assert queue.metadata.toolchains == ("go", "rust")
    assert microservice.metadata.toolchains == ("go",)


def test_bundled_request_factory_package_pins_cargo_git_tool() -> None:
    package = resolve_evaluator_package(
        EvaluatorPackageRequirement(name="vibesys-evaluator-request-factory", version="0.1.0")
    )

    tool = package.metadata.tools["request-factory"]
    assert tool.rev == "118da6137275fda3a290e9012853214dc437c6c0"
    assert tool.package == "req-frontend"
    assert tool.bins == ("session_runner",)
