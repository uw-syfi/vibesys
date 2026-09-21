"""Shared application-level agent client helpers."""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003  # tracked: #288
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class ResponseFallback(Generic[T]):
    """A ``fallback_factory`` that records whether the runner had to use it.

    Every :meth:`AgentClient.invoke` implementation calls ``fallback_factory``
    exactly when the agent's output could not be parsed into ``response_cls``,
    and returns the parsed response otherwise. Wrapping the factory turns that
    framework-side event into a typed signal the caller can read after the
    call, so a loop can tell a response it synthesized apart from one the agent
    actually authored.

    The signal deliberately lives here rather than on the response model: the
    response schema is sent to the agent as its structured-output contract, and
    a framework-only field would leak into it.

    Instances are single-call scoped — construct one per ``invoke()``::

        fallback = ResponseFallback(_missing_implementer_response)
        response = ctx.invoke(..., fallback_factory=fallback)
        if fallback.synthesized:
            ...
    """

    build: Callable[[], T]
    synthesized: bool = field(default=False, init=False)

    def __call__(self) -> T:
        """Build the fallback response and record that the runner needed it."""
        self.synthesized = True
        return self.build()
