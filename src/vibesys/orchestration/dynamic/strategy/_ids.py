"""Deterministic identities the strategy allocates, derived only from scientific state.

Every identity is a pure function of counters and names in the strategy state, so
replaying `decide` after a restart re-proposes the same canonical identities and
core deduplicates them by decision ID and payload digest.
"""

from __future__ import annotations

from vs_core.api import (
    AttemptId,
    AttemptRef,
    DecisionId,
    InvocationId,
    ItemId,
    RoleId,
    SessionId,
)

_OPERATION_PREFIX = "operation:"
_PLAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def component(name: str) -> str:
    """An agent-chosen name as one identity component.

    Core identities hold no whitespace, and a hypothesis ID is free text (spaces,
    slashes, non-ASCII). Percent-encoding is injective and keeps the name readable.
    """
    return "".join(
        char if char in _PLAIN else "".join(f"%{byte:02X}" for byte in char.encode())
        for char in name
    )


def decision_id(kind: str, subject: str, ordinal: int = 0) -> DecisionId:
    """Name one decision by its kind, owning subject and attempt ordinal.

    The subject is raw text; this function encodes it.
    """
    return DecisionId(root=f"dyn:{kind}:{component(subject)}:{ordinal}")


def attempt_id(hypothesis_id: str, sequence: int) -> AttemptId:
    """One attempt per scheduled workstream; a continuation is a new sequence."""
    return AttemptId(root=f"attempt:{component(hypothesis_id)}:{sequence}")


def item_id(work_id: str, sequence: int) -> ItemId:
    """The scientific item an attempt works on."""
    return ItemId(root=f"item:{component(work_id)}:{sequence}")


def attempt_ref(attempt: AttemptId, generation: int) -> AttemptRef:
    """Exact attempt generation."""
    return AttemptRef(attempt_id=attempt, generation=generation)


def invocation_id(role: str, subject: str, serial: int) -> InvocationId:
    """One logical invocation, distinct for every correction and resume.

    The subject is raw text; this function encodes it.
    """
    return InvocationId(root=f"inv:{role}:{component(subject)}:{serial}")


def session_id(role: str, subject: str) -> SessionId:
    """Conversation identity: stable per subject so continuations reuse a session.

    The subject is raw text; this function encodes it.
    """
    return SessionId(root=f"session:{role}:{component(subject)}")


def role_id(role: str) -> RoleId:
    """Agent role identity, matching the role ids in `dynamic.agents`."""
    return RoleId(root=f"dynamic-{role}")


def decision_of_operation(operation_root: str) -> str | None:
    """Invert core's `operation:<decision>` operation identity, or None if foreign."""
    if operation_root.startswith(_OPERATION_PREFIX):
        return operation_root.removeprefix(_OPERATION_PREFIX)
    return None
