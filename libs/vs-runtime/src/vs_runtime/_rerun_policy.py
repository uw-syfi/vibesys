"""What a registered operation may repeat when its step runs again, declared with its owner.

An operation runs in this process, so nothing of it outlives the host except its effect, and the
effect is what a rerun must not repeat. The owner of each operation therefore binds a
``RerunPolicy`` when it enters the catalog (``OperationEntry.owned``), and an entry refuses one its
lifecycle does not allow:

- ``ReadOnly``: the owner reads and writes nothing, so ``clear`` has nothing to remove.
- ``KeyedWrite``: the owner's write is keyed, and its ``inspect`` reads the key back. ``clear``
  asks it, so a write that landed is adopted with its outcome and one that did not is run again.
- ``ClearedWork``: the owner runs a command or leaves files; ``clear`` hands the tag to a
  ``WorkCleaner`` that kills its process group and resets the workspace.
- ``OtherRunner``: a job or a turn. The evaluation and agent-turn runners own its rerun.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_core.api import ContractError, LifecycleClass

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import OperationRegistration, OperationRequest, RequestId


@dataclass(frozen=True)
class ReadOnly:
    """The owner changes nothing, so a rerun needs no clear."""


@dataclass(frozen=True)
class KeyedWrite:
    """The owner's write carries a key its ``inspect`` reads back before a rerun writes."""

    key: str
    """Names the key: what the tracker or store checks before it writes."""

    def __post_init__(self) -> None:
        """A write with no named key is not keyed."""
        if not self.key.strip():
            raise ContractError(("rerun", "key"), "a keyed write names its key")


class WorkCleaner(Protocol):
    """Removes what an interrupted attempt of an operation left in the world."""

    async def clear(self, tag: RequestId, request: OperationRequest) -> None:
        """Kill the processes of ``tag`` and reset what it wrote; return once none remain.

        Raises ``ClearUnavailableError`` when the process table or the workspace cannot be
        reached.
        """
        ...


@dataclass(frozen=True)
class ClearedWork:
    """The owner runs processes or writes files, so a rerun is safe only after the cleaner ran."""

    cleaner: WorkCleaner


@dataclass(frozen=True)
class OtherRunner:
    """A job or a turn: its own runner declares what a rerun repeats, not the operation runner."""


type RerunPolicy = ReadOnly | KeyedWrite | ClearedWork | OtherRunner

_FITS: Mapping[LifecycleClass, tuple[type, ...]] = {
    LifecycleClass.QUERY: (ReadOnly, ClearedWork),
    LifecycleClass.IDEMPOTENT_WRITE: (KeyedWrite, ClearedWork),
    LifecycleClass.OWNED_JOB: (OtherRunner,),
    LifecycleClass.SESSION_TURN: (OtherRunner,),
}


def read_or_keyed(registration: OperationRegistration, key: str) -> ReadOnly | KeyedWrite:
    """The policy of an owner that only reads or only writes under a key: by its lifecycle.

    A query is read only. An idempotent write is keyed by *key*, the key its owner's ``inspect``
    reads back. Any other lifecycle is not this runner's and raises ``ContractError``.
    """
    lifecycle = registration.descriptor.lifecycle
    match lifecycle:
        case LifecycleClass.QUERY:
            return ReadOnly()
        case LifecycleClass.IDEMPOTENT_WRITE:
            return KeyedWrite(key)
        case _:
            raise ContractError(
                ("rerun", registration.descriptor.kind), f"a {lifecycle} operation is read or keyed"
            )


def check_fits(kind: str, lifecycle: LifecycleClass, policy: RerunPolicy) -> None:
    """Raise ``ContractError`` naming the operation when *policy* does not fit its lifecycle."""
    if not isinstance(policy, _FITS[lifecycle]):
        raise ContractError(
            ("rerun", kind), f"{type(policy).__name__} does not fit a {lifecycle} operation"
        )
