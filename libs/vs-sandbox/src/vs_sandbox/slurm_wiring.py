"""Compose the configured cluster for Slurm sandbox execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_slurm.api import SlurmJobRunner
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from pathlib import Path

    from vs_slurm.api import Cluster, SlurmConfig


def make_cluster(config: SlurmConfig, *, state_root: Path) -> Cluster:
    """Wire transport details once; execution depends only on Cluster."""
    state_root.mkdir(parents=True, exist_ok=True)
    return SlurmCluster(
        SlurmJobRunner(config, scratch_root=state_root),
        state_root=state_root,
    )
