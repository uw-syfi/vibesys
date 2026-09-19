from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from vibesys.input_manifest import InputManifest, ProfileGuidedInput, render_input_manifest


def _manifest(profile_guided: object) -> InputManifest:
    return InputManifest.model_validate(
        {
            "version": 1,
            "agent": {"domain": "database"},
            "profile_guided": profile_guided,
            "accuracy": {"command": ["python", "check.py"]},
            "benchmark": {"command": ["python", "benchmark.py"]},
        }
    )


def test_profile_guided_configuration_round_trips_through_manifest_toml() -> None:
    manifest = _manifest(
        {
            "command": ["uv", "run", "python", "profiler/attribute_cpu.py"],
            "timeout_seconds": 900,
            "result_protocol": 1,
            "min_measured_rounds": 3,
            "min_relative_improvement": 0.05,
        }
    )

    rendered = render_input_manifest(manifest)
    reparsed = InputManifest.model_validate(tomllib.loads(rendered))

    assert reparsed == manifest
    assert "[profile_guided]" in rendered


def test_profile_guided_configuration_defaults_are_stable() -> None:
    manifest = _manifest({"command": ["python", "attribute.py"]})

    assert manifest.profile_guided == ProfileGuidedInput(
        command=("python", "attribute.py"),
        timeout_seconds=1800,
        result_protocol=1,
        min_measured_rounds=2,
        min_relative_improvement=0.02,
    )


@pytest.mark.parametrize(
    "profile_guided",
    [
        {"command": []},
        {"command": ["python", ""]},
        {"command": ["python"], "timeout_seconds": 0},
        {"command": ["python"], "result_protocol": 2},
        {"command": ["python"], "min_measured_rounds": 0},
        {"command": ["python"], "min_relative_improvement": -0.01},
    ],
)
def test_profile_guided_configuration_rejects_invalid_values(
    profile_guided: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _manifest(profile_guided)
