"""The schema's conformance fixtures, read and written by the Python side.

``fixtures/`` (indexed by ``manifest.json``) is written by loadgen's
conformance test. The Python side reads every file of it in both formats,
reads each binary file's messages back from what it writes, decodes every
histogram to the manifest's count and percentiles (``check_histograms.py``),
and rejects each file a reader must reject, for the manifest's reason.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# test-isolation: the file codecs and the histogram decoder are the private seam between the
# phase pod and loadgen; the fixtures pin it from both sides.
from vibesys_evaluator._private import hdr
from vibesys_evaluator._private.files import (
    DelimitedReader,
    DelimitedWriter,
    Format,
    SchemaFileError,
    _frames,
    read_requests,
)
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

if TYPE_CHECKING:
    from types import ModuleType

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ENTRIES = json.loads((FIXTURES / "manifest.json").read_text())["entries"]
VALID = [e for e in ENTRIES if e["valid"]]
REJECTED = [e for e in ENTRIES if not e["valid"]]
KINDS = {
    "requests": (pb.FileKind.REQUESTS, pb.RequestsHeader, pb.PlannedOperation),
    "results": (pb.FileKind.RESULTS, None, pb.OperationResult),
    "summary": (pb.FileKind.SUMMARY, pb.PhaseSummary, None),
    "selftest": (pb.FileKind.SELFTEST, pb.SelftestResult, None),
    "capabilities": (pb.FileKind.CAPABILITIES, pb.Capabilities, None),
}


def _read(path: Path, kind: str, file_format: Format) -> list:
    """Every message after the FileHeader, as the package reads the file."""
    if kind == "requests":
        header, operations = read_requests(path, file_format=file_format)
        return [header, *operations]
    file_kind, first, rest = KINDS[kind]
    reader = DelimitedReader(path, file_kind, file_format=file_format)
    messages = [reader.one(first)] if first is not None else []
    if rest is not None:
        messages += list(reader.rest(rest))
    return messages


def test_the_manifest_covers_every_kind_in_both_formats_and_five_rejects() -> None:
    assert {(e["kind"], e["format"]) for e in VALID} == {
        (kind, file_format) for kind in KINDS for file_format in ("binpb", "json")
    }
    assert len(REJECTED) == 5


@pytest.mark.parametrize("entry", VALID, ids=lambda e: e["file"])
def test_a_valid_fixture_is_read(entry: dict) -> None:
    messages = _read(FIXTURES / entry["file"], entry["kind"], entry["format"])
    assert len(messages) == entry["messages"]  # after the FileHeader


@pytest.mark.parametrize(
    "entry", [e for e in VALID if e["format"] == "binpb"], ids=lambda e: e["file"]
)
def test_both_formats_hold_the_same_messages(entry: dict) -> None:
    twin = entry["file"].removesuffix(".binpb") + ".json"
    assert _read(FIXTURES / entry["file"], entry["kind"], "binpb") == _read(
        FIXTURES / twin, entry["kind"], "json"
    )


@pytest.mark.parametrize(
    "entry", [e for e in VALID if e["format"] == "binpb"], ids=lambda e: e["file"]
)
def test_a_binary_fixture_is_written_back_unchanged(entry: dict, tmp_path: Path) -> None:
    # Field order on the wire differs (Go writes oneof fields last), so the written file is
    # compared as messages, as both readers see it.
    path = FIXTURES / entry["file"]
    header = pb.FileHeader.from_binary(bytes(next(_frames(path.read_bytes(), str(path)))))
    messages = _read(path, entry["kind"], "binpb")
    copy = tmp_path / entry["file"]
    with copy.open("wb") as file:
        writer = DelimitedWriter(file)
        writer.write(header)
        for message in messages:
            writer.write(message)
    assert _read(copy, entry["kind"], "binpb") == messages


@pytest.mark.parametrize("entry", REJECTED, ids=lambda e: e["file"])
def test_a_file_a_reader_rejects_is_refused_for_the_manifests_reason(entry: dict) -> None:
    with pytest.raises(SchemaFileError, match=entry["error"]):
        _read(FIXTURES / entry["file"], entry["kind"], entry["format"])


def _check_histograms(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """``fixtures/check_histograms.py``, the fixtures' own checker (not a package)."""
    spec = importlib.util.spec_from_file_location(
        "check_histograms", FIXTURES / "check_histograms.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # its dataclasses look themselves up
    spec.loader.exec_module(module)
    return module


def test_every_fixture_histogram_decodes_to_the_manifests_count_and_percentiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _check_histograms(monkeypatch)

    def decode(encoded: bytes) -> object:
        histogram = hdr.decode(encoded)
        return checker.Decoded(
            count=histogram.total,
            p50=histogram.percentile(50),
            p99=histogram.percentile(99),
            max=histogram.max,
        )

    assert checker.check(decode) == []


@pytest.mark.parametrize("cookie_bits", [0x00, 0x10], ids=["base", "standard"])
def test_both_cookie_forms_decode_and_the_encoder_writes_the_standard_one(
    cookie_bits: int,
) -> None:
    histogram = hdr.Histogram(lowest=1, highest=60_000_000, significant_figures=3)
    histogram.record(1234)
    encoded = bytearray(hdr.encode(histogram))
    assert encoded[:4] == (0x1C849314).to_bytes(4, "big")  # what loadgen and the libraries write
    encoded[3] = (encoded[3] & 0x0F) | cookie_bits  # a reader masks the word-size bits off
    assert hdr.decode(bytes(encoded)).percentile(50) == histogram.percentile(50)
