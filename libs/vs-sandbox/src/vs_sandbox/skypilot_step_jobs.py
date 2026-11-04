"""Cancel the remote jobs a step's agent turn left on a SkyPilot cluster, found by the step's tag.

A SkyPilot job outlives the host that submitted it. The agent of a turn asks for one through the
evaluator bridge, which journals the invocation with the step's tag (``EvaluationRequest.step_tag``).
When the step is run again its earlier attempt's jobs are not wanted: a job that is still running
is cancelled by its cluster and id, found through the journal, so no other step's job is touched
and the cluster, which belongs to the run, stays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_sandbox.skypilot_recovery import InvocationPhase
from vs_sandbox.skypilot_runner import SkyPilotCLIError
from vs_sandbox.skypilot_states import JobEnded

if TYPE_CHECKING:
    from vs_sandbox.skypilot_recovery import InvocationJournal, InvocationRecord
    from vs_sandbox.skypilot_runner import SkyPilotJobRunner

_REMOTE_PHASES = frozenset(
    {InvocationPhase.SUBMITTING, InvocationPhase.SUBMITTED, InvocationPhase.RUNNING}
)
"""Phases that may have a job on the cluster. ``PREPARED`` has none and the rest have ended."""


class JobsSurvivedCancelError(SkyPilotCLIError):
    """A cancelled job was not seen to end, so nothing proves the step's work gone."""

    def __init__(self, invocations: tuple[str, ...]) -> None:
        """Name the invocations whose job still runs."""
        super().__init__(f"jobs of invocations {', '.join(invocations)} are not seen to have ended")
        self.invocations = invocations


def cancel_step_jobs(
    journal: InvocationJournal, runner: SkyPilotJobRunner, tag: str
) -> tuple[str, ...]:
    """Cancel every job of an invocation a step with *tag* asked for that may still run.

    Returns the invocations whose job was cancelled. A job the journal has no id for (a crash
    between the submission and the record of its id) is found by its name. Cancelling a job
    that has ended is not an error, and neither is a tag no invocation carries. After the
    cancel the job is read again: a job not seen to have ended raises
    :class:`JobsSurvivedCancelError`, and the cluster's own ``SkyPilotCLIError`` is raised when
    it cannot be reached, so the caller never reports a clean step it did not see. Both end a
    caller's wait and are retried by the next ``clear``, which cancels again.
    """
    cancelled: list[str] = []
    survivors: list[str] = []
    for record in journal.records():
        if record.step_tag != tag or record.phase not in _REMOTE_PHASES:
            continue
        outcome = _cancel(runner, record)
        if outcome is None:
            continue
        cancelled.append(record.invocation_id)
        if not outcome:
            survivors.append(record.invocation_id)
    if survivors:
        raise JobsSurvivedCancelError(tuple(survivors))
    return tuple(cancelled)


def _cancel(runner: SkyPilotJobRunner, record: InvocationRecord) -> bool | None:
    """Cancel the record's job: ``None`` when it has none, else whether it was seen to end."""
    cluster = record.active_cluster_name
    if cluster is None:
        return None
    job_id = record.remote_job_id
    if job_id is None:
        found = runner.query_job(cluster, job_name=record.job_name)
        if found is None:
            return None
        job_id = found.job_id
    runner.cancel(cluster, job_id)
    after = runner.query_job(cluster, job_name=record.job_name, job_id=job_id)
    return after is None or isinstance(after.life, JobEnded)
