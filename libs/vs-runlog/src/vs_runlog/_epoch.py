"""One pure derivation of writer identity and lifecycle from committed records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_proto.api import Keyframe, Oneof, Record

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Lifecycle


@dataclass(frozen=True)
class CurrentEpoch:
    """Facts published by an epoch; declarations never carry lifecycle authority."""

    number: int = 0
    lifecycle: Lifecycle | None = None


def current_epoch(
    records: Iterable[Record] | Keyframe, seed: CurrentEpoch | None = None
) -> CurrentEpoch:
    """Fold records, accepting lifecycle only from its own epoch's lifecycle records.

    Repeated epoch_started records are legacy declaration amendments. A successor
    starts with no lifecycle; amendments, content and intents cannot change it.
    """
    seed = CurrentEpoch() if seed is None else seed
    if isinstance(records, Keyframe):
        records = sorted(
            [
                Record(seq=row.seq, epoch=row.epoch.epoch, body=Oneof("epoch_started", row.epoch))
                for row in records.epochs
                if row.epoch is not None
            ]
            + [
                Record(seq=row.seq, body=Oneof("lifecycle", row.lifecycle))
                for row in records.lifecycles
                if row.lifecycle is not None
            ],
            key=lambda row: row.seq,
        )
        # Keyframe lifecycle versions inherit the epoch of their preceding header.
        number = 0
        for record in records:
            if record.body is not None and record.body.field == "epoch_started":
                number = record.epoch
            else:
                record.epoch = number
    number = seed.number
    lifecycle = (
        None
        if seed.lifecycle is None
        else type(seed.lifecycle).from_binary(seed.lifecycle.to_binary())
    )
    for record in records:
        body = record.body
        if body is None:
            continue
        if body.field == "epoch_started" and body.value.epoch > number:
            number, lifecycle = body.value.epoch, None
        elif body.field == "lifecycle" and record.epoch == number:
            lifecycle = type(body.value).from_binary(body.value.to_binary())
    return CurrentEpoch(number, lifecycle)
