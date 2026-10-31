"""Compare a committed native replay golden with its projection; a mismatch names its first record.

``assert actual == expected`` over two replays of 300 KB makes pytest build a diff that took up to
803 s with ``-v``, longer than a CI shard lives. This comparison costs one object equality, and a
mismatch reports the first differing record and the command that regenerates the goldens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.bounded_comparison import describe_difference
from tests.support.wire_goldens import REGENERATE

from vs_proto.api import ClientReplay, ClientReplayBatch, Record

if TYPE_CHECKING:
    from pathlib import Path

_NAMES = ("golden", "projection")


def assert_replay_golden(golden: Path, expected: ClientReplay) -> ClientReplay:
    """The decoded golden, which must equal ``expected``; a mismatch raises a short report."""
    actual = ClientReplay.from_binary(golden.read_bytes())
    if actual != expected:
        report = "\n".join(first_difference(actual, expected))
        message = f"{golden.name} is not the current projection: {report}\nRegenerate: {REGENERATE}"
        raise AssertionError(message)
    return actual


def first_difference(actual: ClientReplay, expected: ClientReplay) -> list[str]:
    """Where two different replays first differ, in a few short lines."""
    for index, (got, want) in enumerate(zip(actual.batches, expected.batches, strict=False)):
        if got != want:
            return [f"batch {index},", *_batch_difference(got, want)]
    if len(actual.batches) != len(expected.batches):
        return [f"{len(actual.batches)} batches in the golden, {len(expected.batches)} projected"]
    for name in ("blobs", "streams"):
        got, want = getattr(actual, name), getattr(expected, name)
        for key in sorted(got.keys() | want.keys()):
            if got.get(key) != want.get(key):
                difference = describe_difference(
                    got.get(key, b""), want.get(key, b""), names=_NAMES
                )
                return [f"{name[:-1]} {key!r},", *difference]
    return ["the replays differ in a field this report does not read"]


def _batch_difference(actual: ClientReplayBatch, expected: ClientReplayBatch) -> list[str]:
    if actual.source_sequence != expected.source_sequence:
        return [
            f"source sequence {actual.source_sequence} in the golden, {expected.source_sequence} projected"
        ]
    for index, (got, want) in enumerate(zip(actual.records, expected.records, strict=False)):
        if got != want:
            record = Record.from_binary(want)
            kind = record.body.field if record.body is not None else "empty"
            header = f"record {index} (seq {record.seq}, {kind}), golden against projection:"
            return [
                header,
                *describe_difference(repr(Record.from_binary(got)), repr(record), names=_NAMES),
            ]
    return [f"{len(actual.records)} records in the golden, {len(expected.records)} projected"]
