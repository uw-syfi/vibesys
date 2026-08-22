"""Tests for accelerator device passthrough into the bubblewrap namespace."""

from __future__ import annotations

from pathlib import Path

import pytest

from vs_sandbox import host_sandbox


def _host_devices(pattern: str) -> list[Path]:
    dev = Path("/dev")
    return sorted(dev.glob(pattern)) if dev.exists() else []


class TestAcceleratorDeviceNodes:
    """``--dev`` mounts a minimal devtmpfs, so accelerators need rebinding.

    Without this the agent can edit code for an accelerator it cannot run,
    which fails as a confusing driver error rather than a policy error.
    """

    @pytest.mark.parametrize("pattern", ["neuron*", "nvidia*"])
    def test_host_accelerator_devices_are_passed_through(self, pattern: str) -> None:
        devices = _host_devices(pattern)
        if not devices:
            pytest.skip(f"host has no /dev/{pattern} devices")

        nodes = host_sandbox._gpu_device_nodes()  # noqa: SLF001

        assert set(devices).issubset(set(nodes))

    def test_returns_paths_that_exist(self) -> None:
        nodes = host_sandbox._gpu_device_nodes()  # noqa: SLF001
        assert all(node.exists() for node in nodes)
