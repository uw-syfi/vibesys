"""A run an older version saved, as a record of its run.log, and the reading of a log that holds one.

Core folds plain values. A snapshot record packs the canonical JSON of a saved run
(``encode_saved``) with the state and the search compressed: a saved state is megabytes of JSON,
and the record holds one of them and the other. This module is where that packing happens, and
where a reader of a log turns a snapshot record into the ``SavedRun`` core folds and restores the
search from, so every read of a log goes through ``fold_log`` and ``restore_log``.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_core.api.recovery import (
    EncodedSavedRun,
    apply_record,
    apply_saved,
    decode_saved,
    encode_saved,
    restore,
)
from vs_proto.api import CoreSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_core.api import OperationRegistry, SearchDriver, StrategyState
    from vs_core.api.recovery import Folded, Progress, SavedRun
    from vs_proto.api import Record


def snapshot_message(saved: SavedRun) -> CoreSnapshot:
    """The record body that seeds a log with a run an older version saved."""
    encoded = encode_saved(saved)
    return CoreSnapshot(
        schema_version=encoded.schema_version,
        envelope_version=encoded.envelope_version,
        state=zlib.compress(encoded.state),
        inputs=list(encoded.inputs),
        strategy=zlib.compress(encoded.strategy),
        wakes=list(encoded.wakes),
        held=encoded.held,
    )


def saved_run_in(record: Record, registry: OperationRegistry | None) -> SavedRun | None:
    """The run ``record`` holds if it is a snapshot, None otherwise.

    Raises ``ValueError`` for a snapshot another encoding wrote or damage left unreadable.
    """
    match record.body:
        case Oneof(field="core_snapshot", value=message):
            return decode_saved(_encoded(message), registry)
        case _:
            return None


def apply_logged(
    prior: Folded | None, record: Record, registry: OperationRegistry | None
) -> Folded | None:
    """``prior`` with one more record of a log folded in, a snapshot included."""
    saved = saved_run_in(record, registry)
    if saved is None:
        return apply_record(prior, record, registry)
    return apply_saved(prior, saved, record)


def fold_log(
    records: Iterable[Record], registry: OperationRegistry | None, prior: Folded | None = None
) -> Folded | None:
    """``prior`` with ``records`` folded in, None while the log has not started the run.

    ``prior`` may be the fold of the log's first records (its cache), and ``records`` then the
    ones after.
    """
    folded = prior
    for record in records:
        folded = apply_logged(folded, record, registry)
    return folded


def restore_log[S: StrategyState, C](
    records: Sequence[Record], search: SearchDriver[S, C], registry: OperationRegistry | None
) -> Progress[S] | None:
    """The search the log leaves, from its snapshot if it has one and its advance records."""
    saved = next(
        (found for record in records if (found := saved_run_in(record, registry)) is not None),
        None,
    )
    return restore(records, search, registry, saved=saved)


def _encoded(message: CoreSnapshot) -> EncodedSavedRun:
    return EncodedSavedRun(
        schema_version=message.schema_version,
        envelope_version=message.envelope_version,
        state=_inflated(message.state),
        strategy=_inflated(message.strategy),
        inputs=tuple(message.inputs),
        wakes=tuple(message.wakes),
        held=message.held,
    )


def _inflated(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error as error:
        message = f"the log holds a snapshot that does not inflate: {error}"
        raise ValueError(message) from error
