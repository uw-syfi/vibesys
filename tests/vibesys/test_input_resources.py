from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from vibesys.constants import DomainName
from vibesys.evaluators.input_manifest import InputManifest, render_input_manifest
from vs_project.api import RunResourceRequest


def _manifest(resources: object) -> InputManifest:
    return InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": DomainName.GENERIC},
            "accuracy": {"command": ("python", "check.py")},
            "benchmark": {"command": ("python", "benchmark.py")},
            "resources": resources,
        },
        strict=True,
    )


def _entrypoint_manifest() -> InputManifest:
    return InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": DomainName.GENERIC},
            "accuracy": {"entrypoint": "accuracy.py"},
            "benchmark": {"entrypoint": "benchmark.py"},
            "evaluator": {"name": "sample-evaluator", "version": "1.0"},
        },
        strict=True,
    )


def test_resource_request_round_trips_through_manifest_toml() -> None:
    resources = {
        "nodes": 2,
        "accelerators_per_node": 4,
        "accelerator_backend": "rocm",
        "cpus_per_node": 192,
    }

    rendered = render_input_manifest(_manifest(resources))
    reparsed = InputManifest.model_validate(tomllib.loads(rendered))

    assert reparsed.resources == RunResourceRequest.model_validate(resources, strict=True)
    assert "[resources]" in rendered


def test_render_rechecks_mutated_accuracy_entrypoint() -> None:
    manifest = _entrypoint_manifest()
    manifest.accuracy.entrypoint = None
    with pytest.raises(ValueError, match="accuracy.entrypoint"):
        render_input_manifest(manifest)


def test_render_rechecks_mutated_benchmark_entrypoint() -> None:
    manifest = _entrypoint_manifest()
    manifest.benchmark.entrypoint = ""
    with pytest.raises(ValueError, match="benchmark.entrypoint"):
        render_input_manifest(manifest)


def test_render_rechecks_mutated_evaluator_name() -> None:
    manifest = _entrypoint_manifest()
    assert manifest.evaluator is not None
    manifest.evaluator.name = None
    with pytest.raises(ValueError, match="evaluator.name"):
        render_input_manifest(manifest)


def test_render_rechecks_mutated_evaluator_version() -> None:
    manifest = _entrypoint_manifest()
    assert manifest.evaluator is not None
    manifest.evaluator.version = ""
    with pytest.raises(ValueError, match="evaluator.version"):
        render_input_manifest(manifest)


@pytest.mark.parametrize(
    "resources",
    [
        {
            "nodes": 0,
            "accelerators_per_node": 4,
            "accelerator_backend": "rocm",
        },
        {
            "nodes": 1,
            "accelerators_per_node": 0,
            "accelerator_backend": "rocm",
        },
        {
            "nodes": 1,
            "accelerators_per_node": 4,
            "accelerator_backend": "unknown",
        },
        {
            "nodes": 1,
            "accelerators_per_node": 4,
            "accelerator_backend": "rocm",
            "partition": "site-specific",
        },
    ],
)
def test_resource_request_is_strict(resources: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _manifest(resources)
