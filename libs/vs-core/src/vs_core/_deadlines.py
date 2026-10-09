"""The one rule for how the run deadline bounds a request deadline.

A request whose own deadline lies past the run deadline is not malformed: the
requester stated how long the work may take, and the run may have less time left
than that. Core therefore bounds the deadline of every request kind it issues
(turns, resumed turns, session setup, measurements, registered operations, and
agent waits) to the run deadline with ``bound_to_run``. No request kind
restates the rule, so none can reject or ignore it differently.

A bounded deadline that is not after the run clock has no time left. That is the
only refusal, and each caller reports it as its own typed rejection
(``time_remains``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.strategy import Operation

if TYPE_CHECKING:
    from .types.common import Seconds
    from .types.kernel import RunState
    from .types.strategy import Decision


def bound_to_run(run: RunState, requested: Seconds) -> Seconds:
    """The requested deadline, no later than the run deadline."""
    return min(requested, run.deadline_at)


def time_remains(run: RunState, requested: Seconds) -> bool:
    """Whether any time is left between the run clock and the bounded deadline."""
    return bound_to_run(run, requested) > run.now_at


def has_time_for(run: RunState, decision: Decision) -> bool:
    """Whether the run has time for a decision's own execution deadline.

    Turns and measurements carry their deadline in a nested value and apply
    ``time_remains`` where they admit it; a registered operation carries it on
    the decision itself.
    """
    return not isinstance(decision, Operation) or time_remains(run, decision.deadline_at)


def bounded_decision(run: RunState, decision: Decision) -> Decision:
    """The decision as recorded: an operation's execution deadline bounded to the run.

    Receipts hold this form, so every later check that a request matches its
    decision compares against the bounded deadline without restating the rule.
    """
    if isinstance(decision, Operation) and decision.deadline_at > run.deadline_at:
        return decision.model_copy(update={"deadline_at": bound_to_run(run, decision.deadline_at)})
    return decision
