from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.skypilot.config import (
    ClusterProfileError,
    ClusterProfilesFile,
    SkyPilotProfile,
    load_cluster_profiles,
    resolve_profile,
)
from vs_project import RunResourceRequest

if TYPE_CHECKING:
    from pathlib import Path


def _profile(**overrides: object) -> SkyPilotProfile:
    values: dict[str, object] = {
        "runner": "skypilot",
        "infra": "slurm/example/gpu",
        "accelerator_backend": "rocm",
        "accelerator_type": "MI300A",
        "accelerators_per_node": 4,
        "cpus_per_node": 192,
        "max_nodes": 2,
        "exclusive": True,
        "remote_runtime_image": "docker:rocm/pytorch:test",
        "allocation_time": "08:00:00",
        "remote_artifact_root": "/persistent/vibesys",
    }
    values.update(overrides)
    return SkyPilotProfile.model_validate(values, strict=True)


def _request(**overrides: object) -> RunResourceRequest:
    values: dict[str, object] = {
        "nodes": 1,
        "accelerators_per_node": 4,
        "accelerator_backend": "rocm",
    }
    values.update(overrides)
    return RunResourceRequest.model_validate(values, strict=True)


def test_loads_strict_profile_document(tmp_path: Path) -> None:
    path = tmp_path / "clusters.toml"
    path.write_text(
        """schema_version = 1
[profiles.test]
runner = "skypilot"
infra = "slurm/example/gpu"
accelerator_backend = "rocm"
accelerator_type = "MI300A"
accelerators_per_node = 4
cpus_per_node = 192
max_nodes = 2
exclusive = true
remote_runtime_image = "docker:rocm/pytorch:test"
command_prefix = ["srun", "--overlap"]
allocation_time = "08:00:00"
remote_artifact_root = "/persistent/vibesys"
""",
        encoding="utf-8",
    )

    loaded = load_cluster_profiles(path)

    assert loaded.profiles["test"] == _profile(command_prefix=["srun", "--overlap"])


@pytest.mark.parametrize(
    "text",
    [
        "schema_version = 1\n[profiles.test]\nunknown = true\n",
        "schema_version = 1\nprofiles = []\n",
        "not valid toml = [\n",
    ],
)
def test_profile_load_errors_name_the_path(tmp_path: Path, text: str) -> None:
    path = tmp_path / "clusters.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ClusterProfileError, match=r"clusters\.toml"):
        load_cluster_profiles(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remote_artifact_root", "relative/path"),
        ("remote_artifact_root", "/persistent/../secret"),
        ("allocation_time", "8 hours"),
        ("allocation_time", "1-24:00:00"),
        ("allocation_time", "08:60:00"),
        ("allocation_time", "00:00:00"),
    ],
)
def test_profile_rejects_invalid_paths_and_durations(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _profile(**{field: value})


def test_profile_names_are_bounded_safe_identifiers() -> None:
    with pytest.raises(ValidationError, match="invalid profile name"):
        ClusterProfilesFile(profiles={"Unsafe Profile": _profile()})


@pytest.mark.parametrize("command_prefix", [[], [""], ["srun", ""]])
def test_profile_rejects_empty_command_prefix_entries(
    command_prefix: list[str],
) -> None:
    with pytest.raises(ValidationError):
        _profile(command_prefix=command_prefix)


def test_resolves_portable_request_without_persisting_profile_policy() -> None:
    document = ClusterProfilesFile(profiles={"test": _profile()})

    resolved = resolve_profile(document, "test", _request(cpus_per_node=128))

    assert resolved.profile_name == "test"
    assert resolved.nodes == 1
    assert resolved.accelerators_per_node == 4
    assert resolved.cpus_per_node == 128
    assert resolved.infra == "slurm/example/gpu"


def test_resolves_operator_memory_per_node_not_from_request() -> None:
    document = ClusterProfilesFile(profiles={"test": _profile(memory_gb_per_node=480)})

    resolved = resolve_profile(document, "test", _request())

    assert resolved.memory_gb_per_node == 480


def test_resolves_absent_memory_per_node_when_profile_omits_it() -> None:
    document = ClusterProfilesFile(profiles={"test": _profile()})

    resolved = resolve_profile(document, "test", _request())

    assert resolved.memory_gb_per_node is None


def test_resolution_freezes_operator_command_prefix() -> None:
    document = ClusterProfilesFile(
        profiles={"test": _profile(command_prefix=["srun", "--overlap"])}
    )

    resolved = resolve_profile(document, "test", _request())

    assert resolved.command_prefix == ("srun", "--overlap")


@pytest.mark.parametrize(
    "resource_request",
    [
        _request(accelerator_backend="cuda"),
        _request(nodes=3),
        _request(accelerators_per_node=5),
        _request(cpus_per_node=193),
    ],
)
def test_rejects_requests_exceeding_or_mismatching_profile(
    resource_request: RunResourceRequest,
) -> None:
    document = ClusterProfilesFile(profiles={"test": _profile()})

    with pytest.raises(ClusterProfileError):
        resolve_profile(document, "test", resource_request)


def test_unknown_profile_lists_available_names() -> None:
    document = ClusterProfilesFile(profiles={"test": _profile()})

    with pytest.raises(ClusterProfileError, match="available profiles: test"):
        resolve_profile(document, "missing", _request())
