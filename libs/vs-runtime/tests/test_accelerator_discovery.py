"""Public contract tests for host accelerator discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import (
    AcceleratorDiscovery,
    AcceleratorInventory,
    SystemAcceleratorDiscovery,
)
from vs_runtime.api.testing import FakeAcceleratorDiscovery

if TYPE_CHECKING:
    from pathlib import Path


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _system_inventory(tmp_path: Path) -> tuple[SystemAcceleratorDiscovery, AcceleratorInventory]:
    device_root = tmp_path / "dev"
    for relative in (
        "neuron1",
        "neuron0",
        "neuron_control",
        "kfd",
        "dri/renderD129",
        "dri/renderD128",
    ):
        _touch(device_root / relative)
    rocm_smi = tmp_path / "bin" / "rocm-smi"
    rocm_smi.parent.mkdir()
    rocm_smi.write_text(
        "#!/bin/sh\nprintf 'device,id\\ncard0,0\\ncard1,1\\n'\n",
        encoding="utf-8",
    )
    rocm_smi.chmod(0o755)
    discovery = SystemAcceleratorDiscovery(
        device_root=device_root,
        executable_search_path=str(rocm_smi.parent),
    )
    expected_rocm = AcceleratorInventory(
        device_nodes=(
            str(device_root / "kfd"),
            str(device_root / "dri" / "renderD128"),
            str(device_root / "dri" / "renderD129"),
        ),
        reported_device_count=2,
    )
    return discovery, expected_rocm


def test_system_and_fake_implement_the_same_inventory_contract(tmp_path: Path) -> None:
    system, expected_rocm = _system_inventory(tmp_path)
    expected_trainium = AcceleratorInventory(
        device_nodes=(str(tmp_path / "dev" / "neuron0"), str(tmp_path / "dev" / "neuron1"))
    )
    implementations: tuple[AcceleratorDiscovery, ...] = (
        system,
        FakeAcceleratorDiscovery(trainium=expected_trainium, rocm=expected_rocm),
    )

    for discovery in implementations:
        assert discovery.discover_trainium() == expected_trainium
        assert discovery.discover_rocm() == expected_rocm


def test_rocm_requires_kfd_even_when_render_nodes_exist(tmp_path: Path) -> None:
    _touch(tmp_path / "dev" / "dri" / "renderD128")
    discovery = SystemAcceleratorDiscovery(
        device_root=tmp_path / "dev",
        executable_search_path="",
    )

    assert discovery.discover_rocm() == AcceleratorInventory()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("exit 1\n", id="nonzero-exit"),
        pytest.param("printf '\\n'\n", id="empty-output"),
    ],
)
def test_rocm_utility_failures_are_nonfatal(tmp_path: Path, body: str) -> None:
    _touch(tmp_path / "dev" / "kfd")
    rocm_smi = tmp_path / "bin" / "rocm-smi"
    rocm_smi.parent.mkdir()
    rocm_smi.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    rocm_smi.chmod(0o755)
    discovery = SystemAcceleratorDiscovery(
        device_root=tmp_path / "dev",
        executable_search_path=str(rocm_smi.parent),
    )

    inventory = discovery.discover_rocm()

    assert inventory.device_nodes == (str(tmp_path / "dev" / "kfd"),)
    expected_count = 0 if body.startswith("printf") else None
    assert inventory.reported_device_count == expected_count
