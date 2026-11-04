"""What a step's report says about the job it owns: the evaluation area's reading of a report.

The recovery rule (``_step_attempts``) decides from a step's facts and names no kind of step; the
kinds that own a job are the evaluation area's, so reading their report for the job's end is here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.evaluation import JobObserved, SubmitMeasurement

if TYPE_CHECKING:
    from .types.records import RequestRecord
    from .types.steps import Finished


def ended_job_events(record: RequestRecord, result: Finished) -> tuple[JobObserved, ...]:
    """The job observation of a submission whose own report ended its job, else none.

    A runner that carries a job to its end answers the submission with the job's terminal
    observation, evidence and facts: the report is the job's last observation, and no poll will
    bring another. Without this the job stays unobserved until a poll answers, and a run that is
    ending asks for no poll, so its closure would wait on a release nothing can prove. A runner
    that returns the job's observation beside its report has said it already.
    """
    observation = result.observation
    if (
        not isinstance(record.request, SubmitMeasurement)
        or not (observation.accepted and observation.terminal)
        or observation.resource_id is None
        or any(isinstance(event, JobObserved) for event in result.owner_events)
    ):
        return ()
    report = result.report
    return (
        JobObserved(
            progress=report.progress,
            resource_id=observation.resource_id,
            observation=observation,
            evidence=report.evidence,
            evaluation_result=report.evaluation_result,
            failure=report.measurement_failure,
        ),
    )
