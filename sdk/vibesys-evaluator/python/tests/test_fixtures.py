"""The schema's conformance fixtures, read and written by the Python side.

``fixtures/`` (indexed by ``manifest.json``) is written by loadgen's
conformance test; the Python side must read every binary file of the kinds
it reads, write the same messages back, and reject the files a reader rejects.
JSON (``--format json``) is a debugging format the Python side never reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# test-isolation: the file codecs are the private seam between the phase pod and loadgen;
# the fixtures pin their bytes.
from vibesys_evaluator._private.files import (
    DelimitedReader,
    DelimitedWriter,
    SchemaFileError,
    _frames,
)
from vibesys_evaluator._private.hdr import decode
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ENTRIES = json.loads((FIXTURES / "manifest.json").read_text())["entries"]
KINDS = {
    "requests": (pb.FileKind.REQUESTS, pb.RequestsHeader, pb.PlannedOperation),
    "results": (pb.FileKind.RESULTS, None, pb.OperationResult),
    "summary": (pb.FileKind.SUMMARY, pb.PhaseSummary, None),
    "selftest": (pb.FileKind.SELFTEST, pb.SelftestResult, None),
    "capabilities": (pb.FileKind.CAPABILITIES, pb.Capabilities, None),
}
BINARY = [e for e in ENTRIES if e["format"] == "binpb"]


def _read(entry: dict) -> tuple[pb.FileHeader, list]:
    kind, first, rest = KINDS[entry["kind"]]
    path = FIXTURES / entry["file"]  # an absolute path stays itself
    reader = DelimitedReader(path, kind)
    messages = [reader.one(first)] if first is not None else []
    if rest is not None:
        messages += list(reader.rest(rest))
    header = pb.FileHeader.from_binary(bytes(next(_frames(path.read_bytes(), str(path)))))
    return header, messages


@pytest.mark.parametrize(
    "entry", [e for e in BINARY if e["valid"]], ids=lambda e: e["file"].removesuffix(".binpb")
)
def test_a_valid_fixture_is_read_and_written_back_unchanged(entry: dict, tmp_path: Path) -> None:
    # Field order on the wire differs (Go writes oneof fields last), so the written file is
    # compared as messages, as both readers see it.
    header, messages = _read(entry)
    assert len(messages) == entry["messages"]  # after the FileHeader
    copy = tmp_path / entry["file"]
    with copy.open("wb") as file:
        writer = DelimitedWriter(file)
        writer.write(header)
        for message in messages:
            writer.write(message)
    assert _read({**entry, "file": copy}) == (header, messages)


@pytest.mark.parametrize(
    "entry", [e for e in BINARY if "latency_count" in e], ids=lambda e: e["file"]
)
def test_the_summary_latency_decodes_to_the_fixtures_percentiles(entry: dict) -> None:
    _, (summary,) = _read(entry)
    assert summary.latency is not None
    latency = decode(summary.latency.encoded)
    assert latency.total == entry["latency_count"]
    assert latency.percentile(50.0) == entry["latency_p50_micros"]
    assert latency.percentile(99.0) == entry["latency_p99_micros"]


# What the Python reader checks itself; the count and refs of a plan are loadgen's to reject.
REJECTED = {
    "invalid-schema-version.binpb": "schema version",
    "invalid-kind.binpb": "expected requests",
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_a_file_a_reader_rejects_is_refused(name: str) -> None:
    assert any(e["file"] == name and not e["valid"] for e in ENTRIES)
    with pytest.raises(SchemaFileError, match=REJECTED[name]):
        _read({"file": name, "kind": "requests"})
