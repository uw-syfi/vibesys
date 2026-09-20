"""Typed identity of one reusable agent conversation.

:class:`AgentClient` caches a live session per key and, for the scopes that opt
in, checkpoints that session's provider conversation ID under the same key. The
key therefore decides two things at once: which turns share a conversation, and
which conversations survive a restart.

The serialized form is ``"<scope>:<identifier>"``, e.g. ``"hypothesis:H-01"``,
which is what the machine-local session map is keyed by on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SessionScope(StrEnum):
    """What a reusable agent conversation is scoped to."""

    HYPOTHESIS = "hypothesis"
    """One hypothesis of an agent-loop run, shared by its implementer attempts."""

    CHAT = "chat"
    """One operator chat thread."""

    ROLE = "role"
    """A bare agent role, used when a caller names no narrower conversation."""


#: Scopes whose conversations are checkpointed across processes. A scope opts in
#: only when its conversation belongs to durable run state that a resumed run
#: continues. ``ROLE`` deliberately stays out: it is the fallback key every
#: unscoped call lands on, so persisting it would resume the judge, perf_eval,
#: and profiler conversations of an earlier process against unrelated work.
_DURABLE_SCOPES = frozenset({SessionScope.HYPOTHESIS, SessionScope.CHAT})


@dataclass(frozen=True, slots=True)
class AgentSessionKey:
    """Identity of one reusable agent conversation."""

    scope: SessionScope
    identifier: str

    def __post_init__(self) -> None:
        """Reject identifiers that cannot round-trip through the stored form."""
        if not self.identifier:
            raise ValueError(f"{self.scope.value} session key needs an identifier")  # noqa: TRY003  # tracked: #288

    def __str__(self) -> str:
        """Return the stored form, ``"<scope>:<identifier>"``."""
        return f"{self.scope.value}:{self.identifier}"

    @property
    def durable(self) -> bool:
        """Whether this conversation is checkpointed across processes."""
        return self.scope in _DURABLE_SCOPES

    @classmethod
    def parse(cls, stored: str) -> AgentSessionKey:
        """Parse the stored form, rejecting anything this version cannot own.

        Raises:
            ValueError: if ``stored`` has no scope separator, names a scope this
                version does not define, or carries an empty identifier.
        """
        scope_text, separator, identifier = stored.partition(":")
        if not separator:
            raise ValueError(f"session key is missing a scope prefix: {stored!r}")  # noqa: TRY003  # tracked: #288
        return cls(SessionScope(scope_text), identifier)
