"""Rebuild a retained case from its producer's records, through the previous writer.

Run it from a checkout of the manifest's ``writer_commit``, so ``FileRunLogWriter`` and
``project_head`` are that revision's and not the current ones:

    uv run python proto/testdata/retained/produce.py RECORDS.b64 OUT_DIR

``RECORDS.b64`` holds one base64 ``Record`` per line, exported from the producer named in the
manifest (see README.md). The transform is the one the committed case went through: drop the
spans of the omitted kinds, then make sequences dense. Timestamps, epochs and bodies are kept.
The head's ``process.pid`` is the producing process's, so a rebuild differs from the committed
head in that field only.
"""

import base64
import sys
from pathlib import Path

from vs_proto.api import Record
from vs_runlog.api import FileRunLogWriter
from vs_sim.api.testing import ManualClock, SimThreads

OMITTED_SPAN_KINDS = frozenset({"comparison", "adoption"})


def transform(records: list[Record]) -> list[Record]:
    """Omit comparison and adoption spans, then renumber the rest densely from 1."""
    kept = [r for r in records if not _omitted(r)]
    for seq, record in enumerate(kept, start=1):
        record.seq = seq
    return kept


def _omitted(record: Record) -> bool:
    body = record.body
    if body is None or body.field != "span":
        return False
    span = body.value.span
    return span is not None and span.label is not None and span.label.kind in OMITTED_SPAN_KINDS


def produce(records_file: Path, out: Path) -> None:
    """Write the transformed records through the checkout's writer into ``out``."""
    lines = records_file.read_text().split()
    records = transform([Record.from_binary(base64.b64decode(line)) for line in lines])
    out.mkdir(parents=True, exist_ok=True)
    with FileRunLogWriter(out / "run.log", clock=ManualClock(), threads=SimThreads()) as writer:
        for record in records:
            writer.append(record)
        writer.commit()


if __name__ == "__main__":
    produce(Path(sys.argv[1]), Path(sys.argv[2]))
