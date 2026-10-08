"""Request identities as core knows them, so an executor cannot answer with one it made up.

Core looks an inspection's target up by identity. Every id an observation names must
therefore be one core sent or named. ``CoreRequestId`` is a type only these functions
make; ``ObservationSubject`` and the dispatch record accept nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NewType

from vs_core.api import RequestId

if TYPE_CHECKING:
    from vs_core.api import RequestBase

CoreRequestId = NewType("CoreRequestId", RequestId)

ReceiptKey = NewType("ReceiptKey", str)
"""Where an executor stores a step's receipt: its own business, never an answer."""


def core_identity(request: RequestBase) -> CoreRequestId:
    """The identity core gave *request*, which it sent for execution."""
    if request.request_id is None:
        message = "request_id: execution requires a canonical identity"
        raise ValueError(message)
    return CoreRequestId(request.request_id)


def core_named(request_id: RequestId) -> CoreRequestId:
    """An identity core itself named, such as ``InspectTurn.dispatch`` or a target."""
    return CoreRequestId(request_id)


__all__ = ["CoreRequestId", "ReceiptKey", "core_identity", "core_named"]
