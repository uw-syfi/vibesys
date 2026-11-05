"""Check every histogram in the conformance fixtures against manifest.json, from Python.

loadgen writes histograms in the standard compressed V2 HdrHistogram encoding, the one the
reference libraries (Java, Go, the ``hdrhistogram`` package on PyPI) write: the outer cookie is
``0x1c849304 | 0x10`` and the inner one ``0x1c849303 | 0x10``; a reader compares
``cookie & ~0xf0`` with those bases. This script decodes every histogram of the JSON fixtures
(their binpb twins hold the same messages; the Go tests check that) and compares its count,
50th and 99th percentile (and maximum, with the reference library) with the manifest.

    # with the reference library:
    uv run --no-project --with hdrhistogram==0.10.8 python -I check_histograms.py
    # with the vibesys_evaluator package's own decoder (on its path):
    python check_histograms.py --core

A test calls ``check(decode)`` with its decoder instead.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent
STANDARD_COMPRESSED_COOKIE = 0x1C849304 | 0x10


@dataclass(frozen=True)
class Decoded:
    """What a decoder reports about one histogram; max is None when it cannot say."""

    count: int
    p50: int
    p99: int
    max: int | None


Decoder = Callable[[bytes], Decoded]


def _field(message: dict[str, Any], path: str) -> dict[str, Any]:
    value: Any = message
    for part in path.split("."):
        value = value[int(part)] if part.isdigit() else value[_camel(part)]
    return value


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def histograms() -> Iterator[tuple[str, str, bytes, dict[str, int]]]:
    """Yield (file, field path, encoded bytes, expectation) for every JSON fixture histogram."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        if entry["format"] != "json" or not entry.get("histograms"):
            continue
        lines = (FIXTURES / entry["file"]).read_text(encoding="utf-8").splitlines()
        message = json.loads(lines[1])  # after the FileHeader
        for path, expected in sorted(entry["histograms"].items()):
            encoded = base64.b64decode(_field(message, path)["encoded"])
            yield entry["file"], path, encoded, expected


def check(decode: Decoder) -> list[str]:
    """Return what disagrees with the manifest (empty when everything agrees)."""
    problems = []
    count = 0
    for file, path, encoded, expected in histograms():
        count += 1
        (cookie,) = struct.unpack(">i", encoded[:4])
        if cookie != STANDARD_COMPRESSED_COOKIE:
            problems.append(
                f"{file} {path}: cookie {cookie:#x}, want {STANDARD_COMPRESSED_COOKIE:#x}"
            )
        got = decode(encoded)
        want = Decoded(
            count=expected["count"],
            p50=expected["p50_micros"],
            p99=expected["p99_micros"],
            max=expected["max_micros"] if got.max is not None else None,
        )
        if got != want:
            problems.append(f"{file} {path}: decoded {got}, manifest {want}")
    if count == 0:
        problems.append("no histograms found in manifest.json")
    return problems


def reference_decoder() -> Decoder:
    """The ``hdrhistogram`` package's decoder (it takes the base64 text)."""
    # lint-waiver: LW-731901 [PLC0415]; hdrhistogram is optional: a top-level import would make the --core check need it too.
    from hdrh.histogram import HdrHistogram  # noqa: PLC0415

    def decode(encoded: bytes) -> Decoded:
        histogram = HdrHistogram.decode(base64.b64encode(encoded))
        return Decoded(
            count=histogram.get_total_count(),
            p50=histogram.get_value_at_percentile(50),
            p99=histogram.get_value_at_percentile(99),
            max=histogram.get_max_value() if histogram.get_total_count() else 0,
        )

    return decode


def core_decoder() -> Decoder:
    """The vibesys_evaluator package's own decoder."""
    # lint-waiver: LW-731902 [PLC0415]; vibesys_evaluator is optional: a top-level import would make the reference check need it too.
    from vibesys_evaluator._private import hdr  # noqa: PLC0415

    def decode(encoded: bytes) -> Decoded:
        histogram = hdr.decode(encoded)
        return Decoded(
            count=histogram.total,
            p50=histogram.percentile(50),
            p99=histogram.percentile(99),
            max=None,
        )

    return decode


def main() -> int:
    """Check with the chosen decoder; exit 1 and name each disagreement."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--core", action="store_true", help="use vibesys_evaluator's decoder")
    args = parser.parse_args()
    problems = check(core_decoder() if args.core else reference_decoder())
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"{sum(1 for _ in histograms())} fixture histograms agree with manifest.json")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
