"""FlashInfer timeline tools decode saved buffers exactly and never run candidate code.

Guarantees under test:

* The server registers its discovery, summary and export tools.
* A buffer encoded the way FlashInfer's profiler macros write it decodes to exactly the
  intervals each block recorded, per event name, including timestamps that wrap the
  32-bit timer; exporting it to Chrome JSON and reading that back gives the same summary.
* Per-occurrence skew names the block that finished last.
* Arbitrary bytes, a missing file, an out-of-range ``top`` and an existing export target
  are tool errors, never a normal reply or an overwrite.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
from collections import Counter
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError
from resources.profilers.flashinfer.server import build_server

from vs_sim.api.testing import examples

_NAMES = ("load", "compute", "reduce")
_WRAP = 1 << 32


async def _call_async(name: str, arguments: dict[str, object]) -> dict[str, object]:
    _, structured = await build_server().call_tool(name, arguments)
    return cast("dict[str, object]", structured)


def _call(name: str, **arguments: object) -> dict[str, object]:
    return asyncio.run(_call_async(name, arguments))


def _tag(sm: int, block: int, index: int, kind: int) -> int:
    return (sm << 24) | (block << 12) | (index << 2) | kind


# One writer's events: (event index, gap before it, duration), in nanoseconds.
_WRITER = st.lists(
    st.tuples(
        st.integers(0, len(_NAMES) - 1),
        st.integers(0, 5_000),
        st.integers(1, 50_000),
    ),
    max_size=3,
)


@st.composite
def _buffers(draw: st.DrawFn) -> tuple[bytes, list[list[tuple[int, int, int]]], int]:
    num_blocks = draw(st.integers(1, 4))
    num_groups = draw(st.integers(1, 2))
    writers = [draw(_WRITER) for _ in range(num_blocks * num_groups)]
    if not any(writers):
        writers[0] = [(0, 0, 1)]
    base = draw(st.integers(0, _WRAP - 1))
    stride = num_blocks * num_groups
    words = [0] * (stride * 2 * max(len(events) for events in writers))
    for slot, events in enumerate(writers):
        clock = base + slot
        for k, (index, gap, duration) in enumerate(events):
            begin = clock + gap
            end = begin + duration
            clock = end
            block = slot // num_groups
            for offset, (kind, stamp) in enumerate(((0, begin), (1, end))):
                word = _tag(block % 256, block, index, kind) | ((stamp % _WRAP) << 32)
                words[slot + (2 * k + offset) * stride] = word
    data = struct.pack("<ii", num_blocks, num_groups) + struct.pack(f"<{len(words)}Q", *words)
    return data, writers, num_groups


def _expected(writers: list[list[tuple[int, int, int]]]) -> dict[str, tuple[int, int]]:
    counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for events in writers:
        for index, _, duration in events:
            counts[_NAMES[index]] += 1
            totals[_NAMES[index]] += duration
    return {name: (counts[name], totals[name]) for name in counts}


def _by_event(reply: dict[str, object]) -> dict[str, tuple[int, int]]:
    rows = cast("list[dict[str, object]]", reply["events"])
    return {
        str(row["event"]): (cast("int", row["count"]), round(cast("float", row["total_us"]) * 1e3))
        for row in rows
    }


def test_flashinfer_server_registers_its_tools() -> None:
    names = {tool.name for tool in asyncio.run(build_server().list_tools())}
    assert names == {
        "capabilities",
        "summary",
        "tracks",
        "event_skew",
        "track_window",
        "export_chrome_trace",
    }


@settings(max_examples=examples(pr=8, nightly=200), deadline=None)
@given(buffer=_buffers())
def test_a_buffer_decodes_to_exactly_the_recorded_intervals(
    buffer: tuple[bytes, list[list[tuple[int, int, int]]], int],
) -> None:
    data, writers, _ = buffer
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        path = Path(directory) / "timeline.u64"
        path.write_bytes(data)
        reply = _call("summary", path=str(path), event_names=list(_NAMES), top=10)
        assert reply["unmatched"] == 0
        assert reply["tracks"] == sum(1 for events in writers if events)
        assert _by_event(reply) == _expected(writers)

        exported = Path(directory) / "timeline.json"
        _call("export_chrome_trace", path=str(path), output=str(exported), event_names=list(_NAMES))
        assert _by_event(_call("summary", path=str(exported), top=10)) == _expected(writers)


def test_event_skew_names_the_block_that_finished_last(tmp_path: Path) -> None:
    # Two blocks, one group: block 1 starts 100 ns later and runs 300 ns longer.
    words = [
        _tag(0, 0, 0, 0) | (1_000 << 32),
        _tag(1, 1, 0, 0) | (1_100 << 32),
        _tag(0, 0, 0, 1) | (1_500 << 32),
        _tag(1, 1, 0, 1) | (1_900 << 32),
    ]
    path = tmp_path / "timeline.u64"
    path.write_bytes(struct.pack("<ii", 2, 1) + struct.pack("<4Q", *words))

    reply = _call("event_skew", path=str(path), event="load", event_names=["load"])
    totals = cast("dict[str, float]", reply["totals"])
    assert totals == {
        "wall_us": 0.9,
        "begin_skew_us": 0.1,
        "slowest_us": 0.8,
        "mean_us": 0.65,
        "imbalance_us": 0.15,
    }
    assert reply["last_finishers"] == [{"track": "blk1_g0 sm1", "times_last": 1}]


@settings(max_examples=examples(pr=8, nightly=200), deadline=None)
@given(data=st.binary(max_size=256))
def test_arbitrary_bytes_are_a_reply_or_a_tool_error(data: bytes) -> None:
    with tempfile.TemporaryDirectory(dir="/dev/shm") as directory:
        for suffix in (".u64", ".json"):
            path = Path(directory) / f"timeline{suffix}"
            path.write_bytes(data)
            try:
                reply = _call("summary", path=str(path))
            except ToolError:
                continue
            assert cast("int", reply["intervals"]) > 0


def test_a_missing_timeline_is_a_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match=r"missing\.u64"):
        _call("tracks", path=str(tmp_path / "missing.u64"))


@pytest.mark.parametrize("top", [0, 51])
def test_top_out_of_range_is_a_tool_error(tmp_path: Path, top: int) -> None:
    path = tmp_path / "timeline.u64"
    path.write_bytes(struct.pack("<ii", 1, 1) + struct.pack("<2Q", 1 << 32, 1 | (2 << 32)))
    with pytest.raises(ToolError, match="top must be between"):
        _call("summary", path=str(path), top=top)


def test_export_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "timeline.u64"
    path.write_bytes(struct.pack("<ii", 1, 1) + struct.pack("<2Q", 1 << 32, 1 | (2 << 32)))
    target = tmp_path / "existing.json"
    target.write_text("keep")
    with pytest.raises(ToolError, match=r"new \.json file"):
        _call("export_chrome_trace", path=str(path), output=str(target))
    assert target.read_text() == "keep"
