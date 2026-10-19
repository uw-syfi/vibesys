"""The closed sets of work a run can start and of events core reports, named once.

``WorkKind`` is what a request can ask core for, plus the clock wake-up; ``EventKind`` is the
kinds of event a run's flows receive, past tense, one per thing that happened.
"""

from __future__ import annotations

from enum import StrEnum


class WorkKind(StrEnum):
    """What a ``Wait`` can ask for, plus the clock wake-up."""

    REQUEST_TURN = "request_turn"
    START_ATTEMPT = "start_attempt"
    WITHDRAW = "withdraw"
    MEASURE = "measure"
    PROPOSE_WINNER = "propose_winner"
    OPERATION = "operation"
    WAKE_AT = "wake_at"


class EventKind(StrEnum):
    """The kinds of event a search receives, past tense, one per thing that happened."""

    WORKSPACE_OPENED = "workspace_opened"
    WORKSPACE_CLOSED = "workspace_closed"
    TURN_FINISHED = "turn_finished"
    MEASUREMENT_FINISHED = "measurement_finished"
    OPERATION_FINISHED = "operation_finished"
    ADOPTION_FINISHED = "adoption_finished"
    STOP_REQUESTED = "stop_requested"
    STOP_LIFTED = "stop_lifted"
    TIME_REACHED = "time_reached"
    FAILED = "failed"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_DROPPED = "message_dropped"
    INTERRUPTED = "interrupted"
    TURN_ENDED = "turn_ended"
    """Plumbing of the agent pipeline: one provider turn ended (reply or typed failure)."""
    TURN_YIELDED = "turn_yielded"
    """Plumbing of the agent pipeline: core recorded the wait a turn ended with."""
    YIELD_FAILED = "yield_failed"
    """Plumbing of the agent pipeline: core could not publish the wait a turn ended with."""
    RESUME_READY = "resume_ready"
    """Plumbing of the agent pipeline: core authorized the resume of a suspended turn."""
    RESUME_FAILED = "resume_failed"
    """Plumbing of the agent pipeline: core will never resume a suspended turn."""
    CHECKPOINT_KNOWN = "checkpoint_known"
    """Plumbing of the agent pipeline: core recorded or declined a turn's checkpoint."""
    COMPARISON_SETTLED = "comparison_settled"
    """No run that feeds a comparison is under way any more (``ComparisonSettled``)."""
