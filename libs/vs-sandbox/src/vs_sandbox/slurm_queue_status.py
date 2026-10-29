"""The Slurm ``QueueStatusReader``: the run's own jobs and the contention they wait in.

One read-only scheduler query (``vs_slurm``'s ``read_queue``) lists this user's jobs,
everyone's jobs without identifiers, and each node's GPUs. Of the user's jobs, only the
run's own appear as items: its evaluations (the job ids the run's evaluation cluster
recorded) and its agent GPU jobs (names under the run's own prefix). The same user's
other runs are counted like anyone else's, never listed.

Contention is counted over the partitions the run uses: the evaluation's configured
partitions, the agent GPU partitions, and those its own jobs wait in. With none known,
the whole cluster is counted.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vs_evaluation.api.queue_status import (
    QueueContention,
    QueueItem,
    QueueItemKind,
    QueueItemState,
    QueueKind,
    QueueStatus,
    QueueStatusUnavailable,
)
from vs_sandbox.slurm_executor import evaluation_job_ids
from vs_sim.api import ProcessTimeoutError
from vs_slurm.api import SlurmError, SlurmJobRunner, sbatch_partitions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
    from pathlib import Path

    from vs_sandbox.slurm_policy import SlurmOperatorSettings
    from vs_sim.api import Clock, Processes
    from vs_slurm.api import OwnJob, QueuedJob, QueueSnapshot, SchedulerQueue

_PENDING = "PENDING"
_MAX_WAIT_REASON_CHARS = 64


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    """Seconds from ``earlier`` to ``later`` on the scheduler's clock, never negative."""
    if later is None or earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds())


def _kind(
    job: OwnJob, evaluation_ids: frozenset[str], agent_prefix: str | None
) -> QueueItemKind | None:
    if job.job_id in evaluation_ids:
        return QueueItemKind.EVALUATION
    if agent_prefix is not None and job.name.startswith(f"{agent_prefix}-"):
        return QueueItemKind.AGENT_GPU
    return None


def _jobs_ahead(job: OwnJob, waiting: Iterable[QueuedJob]) -> int | None:
    """Waiting jobs, anyone's, sharing a partition with ``job`` at strictly higher priority."""
    if job.priority is None:
        return None
    return sum(
        1
        for other in waiting
        if other.priority is not None
        and other.priority > job.priority
        and other.partitions & job.partitions
    )


def _item(
    job: OwnJob, kind: QueueItemKind, now: datetime, waiting: tuple[QueuedJob, ...]
) -> QueueItem:
    if job.state == _PENDING:
        return QueueItem(
            id=job.job_id,
            kind=kind,
            state=QueueItemState.WAITING,
            waited_seconds=_seconds(now, job.submitted),
            jobs_ahead=_jobs_ahead(job, waiting),
            estimated_start_in_seconds=_seconds(job.start, now),
            wait_reason=job.reason[:_MAX_WAIT_REASON_CHARS] if job.reason else None,
        )
    return QueueItem(
        id=job.job_id,
        kind=kind,
        state=QueueItemState.RUNNING,
        waited_seconds=_seconds(job.start, job.submitted),
        running_seconds=_seconds(now, job.start),
    )


def queue_status_from_snapshot(
    snapshot: QueueSnapshot,
    *,
    evaluation_job_ids: frozenset[str],
    agent_job_prefix: str | None,
    partitions: frozenset[str],
) -> QueueStatus:
    """The run's view of one scheduler reading; a pure function of its arguments."""
    own = [
        (job, kind)
        for job in snapshot.mine
        if (kind := _kind(job, evaluation_job_ids, agent_job_prefix)) is not None
    ]
    scope = partitions.union(*(job.partitions for job, _ in own))

    def counted(job_partitions: frozenset[str]) -> bool:
        return not scope or bool(job_partitions & scope)

    in_scope = [job for job in snapshot.everyone if counted(job.partitions)]
    waiting = tuple(job for job in in_scope if job.state == _PENDING)
    gpus = {
        node.node: (node.gpus, node.gpus_used)
        for node in snapshot.nodes
        if node.schedulable and counted(frozenset({node.partition}))
    }
    total = sum(count for count, _ in gpus.values())
    busy = min(total, sum(min(count, used) for count, used in gpus.values()))
    return QueueStatus(
        queue=QueueKind.SHARED,
        items=tuple(_item(job, kind, snapshot.now, waiting) for job, kind in own),
        contention=QueueContention(
            jobs_waiting=len(waiting),
            jobs_running=len(in_scope) - len(waiting),
            gpus_total=total,
            gpus_busy=busy,
            gpus_free=total - busy,
        ),
    )


class SlurmQueueStatusReader:
    """Reads a Slurm queue through ``queue``; every failure is ``QueueStatusUnavailable``."""

    def __init__(
        self,
        queue: SchedulerQueue,
        *,
        evaluation_job_ids: Callable[[], frozenset[str]],
        agent_job_prefix: str | None,
        partitions: frozenset[str],
    ) -> None:
        """Bind the scheduler, how to list the run's evaluation jobs, and its partitions.

        ``agent_job_prefix`` is the run's agent GPU job name prefix, ``None`` when the
        run starts no agent GPU jobs.
        """
        self._queue = queue
        self._evaluation_job_ids = evaluation_job_ids
        self._agent_job_prefix = agent_job_prefix
        self._partitions = partitions

    def read(self) -> QueueStatus | QueueStatusUnavailable:
        """One scheduler reading, mapped to the run's view."""
        try:
            snapshot = self._queue.read_queue()
            evaluation_job_ids = self._evaluation_job_ids()
        except (SlurmError, OSError, ValueError, ProcessTimeoutError) as error:
            return QueueStatusUnavailable.because(
                f"the Slurm queue could not be read: {type(error).__name__}: {error}"
            )
        return queue_status_from_snapshot(
            snapshot,
            evaluation_job_ids=evaluation_job_ids,
            agent_job_prefix=self._agent_job_prefix,
            partitions=self._partitions,
        )


def slurm_queue_status_reader(
    settings: SlurmOperatorSettings,
    *,
    run_id: str,
    handle_root: Path,
    processes: Processes,
    clock: Clock,
) -> SlurmQueueStatusReader:
    """The reader of one run on the operator's cluster.

    ``handle_root`` is the run's evaluation executor's (its submitted jobs are the run's
    evaluations), and the run's agent GPU jobs carry ``AgentGpuConfig.for_run``'s prefix,
    the one its GPU commands are started with.
    """
    agent_gpu = settings.agent_gpu if settings.agent_gpu_shell_command is not None else None
    partitions = sbatch_partitions(settings.config.sbatch_arguments)
    if agent_gpu is not None:
        partitions |= frozenset(agent_gpu.partitions)
    return SlurmQueueStatusReader(
        SlurmJobRunner(settings.config, processes=processes, clock=clock),
        evaluation_job_ids=partial(evaluation_job_ids, handle_root),
        agent_job_prefix=None if agent_gpu is None else agent_gpu.for_run(run_id).job_name_prefix,
        partitions=partitions,
    )


__all__ = ["SlurmQueueStatusReader", "queue_status_from_snapshot", "slurm_queue_status_reader"]
