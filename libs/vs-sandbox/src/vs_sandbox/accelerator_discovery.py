"""Host accelerator discovery mechanisms for compute backends."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import SubprocessProbe

if TYPE_CHECKING:
    from vs_sim.api import CommandProbe


@dataclass(frozen=True, slots=True)
class AcceleratorInventory:
    """Policy-neutral device facts observed on one host."""

    device_nodes: tuple[str, ...] = ()
    reported_device_count: int | None = None


class AcceleratorDiscovery(Protocol):
    """Discover accelerator device facts without selecting how to use them."""

    def discover_trainium(self) -> AcceleratorInventory:
        """Return the host's numbered AWS Neuron device nodes."""
        ...

    def discover_rocm(self) -> AcceleratorInventory:
        """Return the host's AMD device nodes and optional reported GPU count."""
        ...


class SystemAcceleratorDiscovery:
    """Inspect Linux device nodes and vendor utilities on the current host."""

    def __init__(
        self,
        *,
        device_root: Path = Path("/dev"),
        executable_search_path: str | None = None,
        probe: CommandProbe | None = None,
    ) -> None:
        """Configure the filesystem and command seams used for discovery."""
        self._probe: CommandProbe = probe or SubprocessProbe()
        self._device_root = Path(device_root)
        self._executable_search_path = executable_search_path

    def discover_trainium(self) -> AcceleratorInventory:
        """Return sorted numbered ``neuron`` device nodes, excluding control nodes."""
        prefix = "neuron"
        nodes = tuple(
            sorted(
                str(path)
                for path in self._device_root.glob("neuron*")
                if path.name[len(prefix) :].isdigit()
            )
        )
        return AcceleratorInventory(device_nodes=nodes)

    def discover_rocm(self) -> AcceleratorInventory:
        """Return ``kfd`` plus render nodes when the AMD compute driver is present."""
        kfd = self._device_root / "kfd"
        if not kfd.exists():
            return AcceleratorInventory()
        render_nodes = tuple(
            sorted(str(path) for path in (self._device_root / "dri").glob("render*"))
        )
        return AcceleratorInventory(
            device_nodes=(str(kfd), *render_nodes),
            reported_device_count=self._query_rocm_device_count(),
        )

    def _query_rocm_device_count(self) -> int | None:
        rocm_smi = shutil.which("rocm-smi", path=self._executable_search_path)
        if rocm_smi is None:
            return None
        result = self._probe.run([rocm_smi, "--showid", "--csv"], timeout_seconds=10)
        if result is None:
            return None
        if result.returncode != 0:
            return None
        rows = [line for line in result.stdout.splitlines() if line.strip()]
        return max(len(rows) - 1, 0)


__all__ = ["AcceleratorDiscovery", "AcceleratorInventory", "SystemAcceleratorDiscovery"]
