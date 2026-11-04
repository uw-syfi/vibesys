"""Generate language-neutral run.log framing fixtures. Run from the repository root."""

from __future__ import annotations

import json
from pathlib import Path

from vs_proto.api import (
    RUN_LOG_MAGIC,
    RUN_LOG_MAX_RECORD_BYTES,
    EpochStarted,
    LegacyEvent,
    Oneof,
    Record,
    Timestamp,
)
from vs_runlog.api import encode_frame


def _length_prefix(value: int) -> bytes:
    prefix = bytearray()
    continuation_bit = 128
    while value >= continuation_bit:
        prefix.append((value & 127) | continuation_bit)
        value >>= 7
    prefix.append(value)
    return bytes(prefix)


def fixtures() -> dict[str, bytes]:
    """Return every deterministic fixture and its expected-record manifest."""
    epoch = EpochStarted(
        run_id="run-fixture",
        epoch=1,
        tree_hash="tree-fixture",
        dirty=True,
        api_schema_hash="api-fixture",
        store_schema_hash="store-fixture",
    )
    records = [
        Record(
            seq=1, at=Timestamp(seconds=123, nanos=456), epoch=1, body=Oneof("epoch_started", epoch)
        ),
        Record(
            seq=2,
            at=Timestamp(seconds=124),
            epoch=1,
            body=Oneof("legacy_event", LegacyEvent(json='{"text":"hello"}')),
        ),
    ]
    first, second = (encode_frame(record) for record in records)
    damaged = bytearray(second)
    damaged[-1] ^= 1
    files = {
        "valid.runlog": RUN_LOG_MAGIC + first + second,
        "torn-tail.runlog": RUN_LOG_MAGIC + first + second[:-1],
        "bad-crc.runlog": RUN_LOG_MAGIC + first + bytes(damaged),
        "over-cap.runlog": RUN_LOG_MAGIC + _length_prefix(RUN_LOG_MAX_RECORD_BYTES + 1),
        "empty.runlog": RUN_LOG_MAGIC,
        "bad-magic.runlog": b"BADMAGIC",
    }
    expected = [
        {
            "seq": 1,
            "at": {"seconds": 123, "nanos": 456},
            "epoch": 1,
            "epoch_started": {
                "run_id": "run-fixture",
                "epoch": 1,
                "tree_hash": "tree-fixture",
                "dirty": True,
                "api_schema_hash": "api-fixture",
                "store_schema_hash": "store-fixture",
            },
        },
        {
            "seq": 2,
            "at": {"seconds": 124, "nanos": 0},
            "epoch": 1,
            "legacy_event": {"json": '{"text":"hello"}'},
        },
    ]
    boundary = len(RUN_LOG_MAGIC + first)
    manifest = {
        "valid.runlog": {"records": expected, "stop": "end", "offset": len(files["valid.runlog"])},
        "torn-tail.runlog": {"records": expected[:1], "stop": "torn_tail", "offset": boundary},
        "bad-crc.runlog": {"records": expected[:1], "stop": "corruption", "offset": boundary},
        "over-cap.runlog": {"records": [], "stop": "corruption", "offset": len(RUN_LOG_MAGIC)},
        "empty.runlog": {"records": [], "stop": "end", "offset": len(RUN_LOG_MAGIC)},
        "bad-magic.runlog": {"records": [], "stop": "corruption", "offset": 0},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    return files


def main() -> None:
    """Write fixtures beside this generator."""
    for name, contents in fixtures().items():
        Path(__file__).with_name(name).write_bytes(contents)


if __name__ == "__main__":
    main()
