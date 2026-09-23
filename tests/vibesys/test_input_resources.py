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
