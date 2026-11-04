"""The run's read-only queue status: which reader its environment gets, and where it is served.

The host owns the backend reader, because only the host can reach the scheduler; agents
reach it through the queue status tool server, which reads this service over a unix
socket beside the evaluation socket (one mounted directory serves both). One cached
reading serves every agent for ``FRESH_FOR_S`` seconds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_evaluation.api.queue_status import (
    FRESH_FOR_S,
    CachedQueueStatusReader,
    LocalQueueStatusReader,
    OpaqueQueueStatusReader,
    QueueStatusService,
)
from vs_sandbox.api.slurm import slurm_queue_status_reader

if TYPE_CHECKING:
    from pathlib import Path

    from vs_evaluation.api.queue_status import QueueStatusReader
    from vs_runtime.api.infrastructure import RunEnvironmentView
    from vs_sim.api import Clock, Network, Processes, Threads

#: Run environments whose evaluations wait in a service that reports no queue.
_OPAQUE_ENVIRONMENTS = frozenset({"modal", "skypilot"})


def queue_status_reader(
    view: RunEnvironmentView,
    *,
    run_id: str,
    handle_root: Path,
    processes: Processes,
    clock: Clock,
) -> QueueStatusReader:
    """The reader for the run's environment: its Slurm cluster, an opaque service, or this host.

    ``handle_root`` is the cluster evaluation executor's, whose records name the run's
    evaluation jobs; ``processes`` reach the cluster as the executor's do.
    """
    cluster = view.cluster_evaluation
    if cluster is not None:
        return slurm_queue_status_reader(
            cluster.settings,
            run_id=run_id,
            handle_root=handle_root,
            processes=processes,
            clock=clock,
        )
    if view.env_kind in _OPAQUE_ENVIRONMENTS:
        return OpaqueQueueStatusReader()
    return LocalQueueStatusReader()


def queue_status_service(
    reader: QueueStatusReader,
    socket_path: Path,
    *,
    network: Network,
    threads: Threads,
    clock: Clock,
) -> QueueStatusService:
    """The service of ``reader``, cached for ``FRESH_FOR_S``, at ``socket_path``; not yet started.

    The caller prepares the socket's private directory (``prepare_queue_status_socket``),
    registers ``close`` with the run's release stack, then starts it.
    """
    return QueueStatusService(
        CachedQueueStatusReader(reader, clock=clock, threads=threads, fresh_for_s=FRESH_FOR_S),
        socket_path,
        network=network,
        threads=threads,
        clock=clock,
    )


__all__ = ["queue_status_reader", "queue_status_service"]
