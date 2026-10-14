"""What a waiting entity waits on, recorded by the transition that made it wait."""

from __future__ import annotations

from pydantic import model_validator

from .common import ContractValidationError, RequestId, Seconds, Value


class WaitOn(Value):
    """The requests and the timer one waiting entity waits on; any one of them wakes it.

    The transition that makes an entity wait writes this value in the same step, naming
    exactly what can end the wait: requests by identity, and a core time the shell's clock
    reaches. Nothing is inferred later. Applying an answer of one of the requests wakes the
    waiter, which moves on or waits again; a waiter that needs several answers waits again on
    the ones still open. A wait on nothing is no wait, so an empty one is refused.
    """

    requests: tuple[RequestId, ...] = ()
    timer: Seconds | None = None
    """The core time at which the waiter acts by itself, or None."""

    @model_validator(mode="after")
    def names_something(self) -> WaitOn:
        """A wait names at least one dependency, each request once."""
        if not self.requests and self.timer is None:
            raise ContractValidationError("requests", "a wait names a request or a timer")
        if len(set(self.requests)) != len(self.requests):
            raise ContractValidationError("requests", "a wait names each request once")
        return self
