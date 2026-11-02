"""What a waiting entity waits on, recorded by the transition that made it wait."""

from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from .common import ContractValidationError, RequestId, Seconds, Value


class WaitKind(StrEnum):
    """What is waiting."""

    SESSION = "session"
    OWNED_JOB = "owned-job"
    RELEASE_DEPENDENCY = "release-dependency"
    YIELD_PUBLICATION = "yield-publication"
    INTERRUPT = "interrupt"
    REFUND = "refund"
    DISPATCH = "dispatch"
    RETRY = "retry"
    TURN_ACQUISITION = "turn-acquisition"
    BLOCKED_RECORD = "blocked-record"


class WaitSubject(Value):
    """One waiting entity: what kind of wait it is and the identity ``OpenWait.subject`` names."""

    kind: WaitKind
    identity: str


class WaitOn(Value):
    """The requests, waiting entities and timer one waiter waits on; any one of them wakes it.

    The transition that makes an entity wait writes this value in the same step, naming
    exactly what can end the wait: requests by identity, other waiting entities whose own
    progress carries this one (``subjects``), and a core time the shell's clock reaches.
    Nothing is inferred later. Applying an answer of one of the requests wakes the waiter,
    which moves on or waits again; a waiter that needs several answers waits again on the ones
    still open. A named entity carries the waiter only while its own recorded wait does, and
    which kinds may name which is fixed and acyclic (``MAY_WAIT_ON`` in ``_waits``), so the
    chain of waits always ends at a request or a timer. A wait on nothing is no wait, so an
    empty one is refused.
    """

    requests: tuple[RequestId, ...] = ()
    subjects: tuple[WaitSubject, ...] = ()
    timer: Seconds | None = None
    """The core time at which the waiter acts by itself, or None."""

    @model_validator(mode="after")
    def names_something(self) -> WaitOn:
        """A wait names at least one dependency, each one once."""
        if not self.requests and not self.subjects and self.timer is None:
            raise ContractValidationError(
                "requests", "a wait names a request, a waiting entity or a timer"
            )
        if len(set(self.requests)) != len(self.requests):
            raise ContractValidationError("requests", "a wait names each request once")
        if len(set(self.subjects)) != len(self.subjects):
            raise ContractValidationError("subjects", "a wait names each entity once")
        return self
