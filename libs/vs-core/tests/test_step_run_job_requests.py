"""A job whose first observation is its end draws one collection and nothing else about the job.

A step that runs a measurement returns the job whole, once it ended: core never sees the job
running, so it has no poll or cancel to send. What it asks of the executor is the collection of
evidence an ended job brought none of, and (at the attempt's closure) the close of the scope.
The runtime registers a step-run job so the executor answers those; the other two job requests
need no registration because core never sends them for such a job.
"""

from __future__ import annotations

import pytest

import vs_core.api as core

from .test_cancel_stop_run_jobs import _Run

ENDS = (
    core.ObservationStatus.SUCCEEDED,
    core.ObservationStatus.FAILED,
    core.ObservationStatus.CANCELLED,
)


@pytest.mark.parametrize("status", ENDS)
def test_a_job_whose_first_observation_is_its_end_draws_only_a_collection(
    status: core.ObservationStatus,
) -> None:
    run = _Run(1)
    run.observe(0, status, accepted=True, terminal=True, released=True, children_complete=True)
    kinds = {type(request) for request in run.sent}
    assert core.CollectEvidence in kinds
    assert kinds <= {core.CollectEvidence}
