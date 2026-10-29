"""Apply a replay run's inbox controls: the durable disposition first, then the inbox mark."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api.replay import stale_control
from vs_proto.api import ValidationError, validate
from vs_proto.api import replay as pb

if TYPE_CHECKING:
    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

    from vs_project.api import ControlInbox, InboxEntry
    from vs_runtime._replay_runtime import ReplayRuntime

_STALE = {
    pb.ReplayControlStale.EPOCH: "stale control: it was written for another epoch",
    pb.ReplayControlStale.TURN: "stale control: its turn has ended",
}


def apply_controls(
    runtime: ReplayRuntime, inbox: ControlInbox, *, now: float, at: Timestamp
) -> tuple[pb.ReplayControlDisposition, ...]:
    """Dispose of every pending control, in posting order.

    The runtime records each disposition before its entry is settled or rejected,
    so a crash between the two leaves the entry pending and the retry returns the
    recorded disposition. An entry that is not a well-formed control is rejected
    with the reason. Pause-now withdrawals are durable when this returns;
    cancellation follows through retirement.
    """
    disposed = []
    for entry in inbox.pending():
        control = _control(entry)
        if isinstance(control, str):
            inbox.reject(entry.request_id, control)
            continue
        disposition = runtime.control(control, now=now, at=at)
        stale = stale_control(disposition)
        if stale is None:
            inbox.settle(entry.request_id)
        else:
            inbox.reject(entry.request_id, _STALE[stale])
        disposed.append(disposition)
    return tuple(disposed)


def _control(entry: InboxEntry) -> pb.ReplayControl | str:
    if entry.unreadable is not None:
        return entry.unreadable
    try:
        control = pb.ReplayControl.from_binary(entry.payload)
        validate(control)
    except (ValueError, ValidationError) as error:
        return f"not a replay control: {error}"
    if control.to_binary() != control.to_binary(write_unknown_fields=False):
        return "replay control has unknown fields"
    if control.request_id != entry.request_id:
        return "replay control names another request id"
    return control
