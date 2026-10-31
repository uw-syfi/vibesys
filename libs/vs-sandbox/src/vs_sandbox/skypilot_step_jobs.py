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

if TYPE_CHECKING:
    from vs_sandbox.skypilot_recovery import InvocationJournal, InvocationRecord
    from vs_sandbox.skypilot_runner import SkyPilotJobRunner

_REMOTE_PHASES = frozenset(
    {InvocationPhase.SUBMITTING, InvocationPhase.SUBMITTED, InvocationPhase.RUNNING}
)
"""Phases that may have a job on the cluster. ``PREPARED`` has none and the rest have ended."""


def cancel_step_jobs(
    journal: InvocationJournal, runner: SkyPilotJobRunner, tag: str
) -> tuple[str, ...]:
    """Cancel every job of an invocation a step with *tag* asked for that may still run.

    Returns the invocations whose job was cancelled. A job the journal has no id for (a crash
    between the submission and the record of its id) is found by its name. Cancelling a job
    that has ended is not an error, and neither is a tag no invocation carries. Raises the
    runner's ``SkyPilotCLIError`` when the cluster cannot be reached: nothing is then proven.
    """
    return tuple(
        record.invocation_id
        for record in journal.records()
        if record.step_tag == tag and record.phase in _REMOTE_PHASES and _cancel(runner, record)
    )


def _cancel(runner: SkyPilotJobRunner, record: InvocationRecord) -> bool:
    cluster = record.active_cluster_name
    if cluster is None:
        return False
    job_id = record.remote_job_id
    if job_id is None:
        found = runner.query_job(cluster, job_name=record.job_name)
        if found is None:
            return False
        job_id = found.job_id
    runner.cancel(cluster, job_id)
    return True
