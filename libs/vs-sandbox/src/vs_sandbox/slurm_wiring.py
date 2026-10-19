"""Compose the configured cluster for Slurm sandbox execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_slurm.api import RemoteEvaluationLedger, SlurmJobRunner
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Clock, Processes
    from vs_slurm.api import Cluster, EvaluationLedger, SlurmConfig


def make_cluster(
    config: SlurmConfig,
    *,
    state_root: Path,
    clock: Clock,
    processes: Processes,
) -> Cluster:
    """Wire transport details once; execution depends only on Cluster.

    ``processes`` starts the transport's programs; a Fake answers them without a process.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    return SlurmCluster(
        SlurmJobRunner(config, scratch_root=state_root, processes=processes, clock=clock),
        state_root=state_root,
    )


def make_evaluation_ledger(
    config: SlurmConfig,
    *,
    project: str,
    state_root: Path,
    processes: Processes,
    clock: Clock,
) -> EvaluationLedger:
    """The project's evaluation ledger, on the cluster's filesystem beside its jobs (#2757)."""
    state_root.mkdir(parents=True, exist_ok=True)
    return RemoteEvaluationLedger(
        config, project=project, processes=processes, clock=clock, scratch_root=state_root
    )
