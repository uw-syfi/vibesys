"""What ``vs_slurm`` says about a cluster of the machine's compute settings.

``slurm_config_of`` is the one place a ``SlurmClusterSettings`` becomes the ``SlurmConfig`` a run
loads, so a cluster saved by the helper is one a run can use; ``operator_document`` is that
configuration as the ``[slurm]`` table of the operator file, and ``operator_cluster`` reads such a
table back as settings. Kept apart from ``server.host_compute_runtime`` so the helper loads
``vs_slurm`` only for a compute request, not for ``hello``.

Public surface: ``slurm_config_of``, ``operator_document``, ``operator_cluster``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.host_compute import LoginNodeReach, SlurmClusterSettings, SshReach
from vs_slurm.api import SlurmConfig, SlurmLocalTransport, SlurmSshTransport

if TYPE_CHECKING:
    from collections.abc import Mapping


def slurm_config_of(cluster: SlurmClusterSettings) -> SlurmConfig:
    """The ``vs_slurm`` configuration ``cluster`` stands for; ``ValueError`` names an invalid key.

    The cluster's partition, node count, time limit and further options become its ``sbatch``
    arguments, in that order.
    """
    arguments: list[str] = []
    if cluster.partition is not None:
        arguments.append(f"--partition={cluster.partition}")
    if cluster.nodes is not None:
        arguments.append(f"--nodes={cluster.nodes}")
    if cluster.time_limit is not None:
        arguments.append(f"--time={cluster.time_limit}")
    arguments.extend(cluster.sbatch_options)
    transport: dict[str, object] = (
        {"kind": "ssh", "host": cluster.reach.host}
        if isinstance(cluster.reach, SshReach)
        else {"kind": "local"}
    )
    return SlurmConfig.model_validate(
        {
            "name": cluster.name,
            "remote_workspace_root": cluster.workspace,
            "transport": transport,
            "sbatch_arguments": arguments,
        },
        strict=True,
    )


def operator_document(config: SlurmConfig) -> dict[str, object]:
    """``config`` as the operator file's document: one ``[slurm]`` table."""
    return {"slurm": config.model_dump(mode="json", exclude_none=True)}


def operator_cluster(table: Mapping[str, object]) -> SlurmClusterSettings | None:
    """The cluster an operator file's ``[slurm]`` table describes, its ``sbatch`` arguments as written.

    ``None`` for a connector transport, which is reached by a program of the operator's and is
    neither an ssh alias nor a login node. ``ValueError`` for a table ``SlurmConfig`` rejects.
    """
    config = SlurmConfig.model_validate(table, strict=True)
    transport = config.transport
    if isinstance(transport, SlurmSshTransport):
        reach: SshReach | LoginNodeReach = SshReach(host=transport.host)
    elif isinstance(transport, SlurmLocalTransport):
        reach = LoginNodeReach()
    else:
        return None
    return SlurmClusterSettings(
        name=config.name,
        reach=reach,
        workspace=config.remote_workspace_root,
        sbatch_options=config.sbatch_arguments,
    )
