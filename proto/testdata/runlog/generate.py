"""Generate language-neutral run.log framing fixtures. Run from the repository root."""

from __future__ import annotations

import json
from pathlib import Path

from vs_proto.api import LegacyEvent, Oneof, Record
from vs_runlog.api import MAGIC, encode_frame


def fixtures() -> dict[str, bytes]:
    """Return every deterministic fixture and its expected-record manifest."""
    records = [
        Record(
            seq=1, epoch=1, body=Oneof("legacy_event", LegacyEvent(json='{"type":"run_started"}'))
        ),
        Record(seq=2, epoch=1, body=Oneof("legacy_event", LegacyEvent(json='{"text":"hello"}'))),
    ]
    first, second = (encode_frame(record) for record in records)
    damaged = bytearray(first)
    damaged[-1] ^= 1
    files = {
        "valid.runlog": MAGIC + first + second,
        "torn-tail.runlog": MAGIC + first + second[:-1],
        "bad-crc.runlog": MAGIC + bytes(damaged),
        "over-cap.runlog": MAGIC + b"\x81\x80\x40",
        "empty.runlog": MAGIC,
    }
    expected = [
        {"seq": record.seq, "epoch": record.epoch, "legacy_event": {"json": record.body.value.json}}
        for record in records
        if record.body is not None
    ]
    manifest = {
        "valid.runlog": {"records": expected, "stop": "end", "offset": len(files["valid.runlog"])},
        "torn-tail.runlog": {
            "records": expected[:1],
            "stop": "torn_tail",
            "offset": len(MAGIC + first),
        },
        "bad-crc.runlog": {"records": [], "stop": "corruption", "offset": len(MAGIC)},
        "over-cap.runlog": {"records": [], "stop": "corruption", "offset": len(MAGIC)},
        "empty.runlog": {"records": [], "stop": "end", "offset": len(MAGIC)},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    return files


def main() -> None:
    """Write fixtures beside this generator."""
    for name, contents in fixtures().items():
        Path(__file__).with_name(name).write_bytes(contents)


if __name__ == "__main__":
    main()
