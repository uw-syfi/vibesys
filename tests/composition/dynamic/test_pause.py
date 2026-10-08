"""A pause holds the run; it never ends it.

The operator pauses a run to look at it, then resumes it. Turns and jobs already running
keep going while the run is paused, and what they finish with must still be acted on after
the resume. The scenario is the production shell and loop on virtual time
(`tests/support/scale_dynamic_run.py`) with the pause in the middle of the implementer
turns, so the turns finish while the run is paused.
"""

from __future__ import annotations

from tests.support.scale_dynamic_run import Scenario, run_scale
from tests.support.timed_dynamic_run import TimingProfile

from vs_core.api import RunStatus
from vs_slurm.api import SecondsRange, SlurmTimingProfile

_EXACT = TimingProfile(
    turn_s=SecondsRange.exactly(40.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange.exactly(30.0),
        run_s=SecondsRange.exactly(150.0),
        completing_s=SecondsRange.exactly(35.0),
    ),
)


def test_turns_that_finish_during_a_pause_are_followed_up_after_the_resume() -> None:
    reference = run_scale(Scenario(in_flight=2, rounds=1, profile=_EXACT))
    assert reference.error is None
    assert reference.core.run.result is not None
    assert reference.core.run.result.outcome == "success"
    implementers = [span for span in reference.turns if span.role == "implementer"]
    pause = (implementers[0].start + implementers[0].end) / 2 - reference.started_at
    resume = max(span.end for span in implementers) + 300.0 - reference.started_at

    run = run_scale(
        Scenario(in_flight=2, rounds=1, profile=_EXACT, pause_after=pause, resume_after=resume)
    )

    assert run.paused_at is not None
    assert run.error is None, run.error
    assert run.core.run.status is RunStatus.TERMINAL
    # The run ended after the resume, the way the unpaused run did.
    assert run.ended_at >= resume + run.started_at
    assert run.core.run.result == reference.core.run.result, [
        (receipt.decision_id.root, receipt.feedback)
        for receipt in run.core.run.receipts
        if type(receipt.feedback).__name__ == "Rejected"
    ]
