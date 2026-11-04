"""MCP tools for per-block analysis of intra-kernel timelines.

A kernel instrumented with FlashInfer's profiler macros (``include/flashinfer/profiler.cuh``,
enabled by ``FLASHINFER_ENABLE_PROFILER``) records begin and end events per thread block
into a ``uint64`` buffer. Capture is a shell command the profiler agent runs in the
candidate's execution environment; it saves that buffer raw. This server only reads the
saved buffer, or a Chrome trace-event JSON file, and never runs candidate code.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(_HERE))
# lint-waiver: LW-994900 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
import flashinfer_trace  # noqa: E402

# The same common runtime is staged beside each standalone profiler bundle.
for _common_name in ("_common", "profilers_common"):
    _common_path = _HERE.parent / _common_name
    if (_common_path / "capture_runtime.py").is_file():
        sys.path.insert(0, str(_common_path))
        break
capture_runtime = importlib.import_module("capture_runtime")
StrictFastMCP = importlib.import_module("strict_arguments").StrictFastMCP

_MAX_INPUT_BYTES = 256 * 1024 * 1024
_MAX_TOP = 50
_WINDOW_LIMIT = 200


def _fail(diagnostic: object) -> Exception:
    return capture_runtime.CaptureFailedError.analysis_failed(diagnostic)


def _read_input(path: str) -> bytes:
    artifact = Path(path)
    if not artifact.is_file():
        message = f"timeline file does not exist or is not a regular file: {path}"
        raise _fail(message)
    size = artifact.stat().st_size
    if size > _MAX_INPUT_BYTES:
        message = f"{path} is {size} bytes; narrow the capture below {_MAX_INPUT_BYTES} bytes"
        raise _fail(message)
    return artifact.read_bytes()


def _load(
    path: str, event_names: list[str] | None, track_filter: str = ""
) -> flashinfer_trace.Timeline:
    data = _read_input(path)
    try:
        if Path(path).suffix == ".json":
            timeline = flashinfer_trace.decode_chrome_trace(data)
        else:
            timeline = flashinfer_trace.decode_buffer(data, tuple(event_names or ()))
        return timeline.select(track_filter)
    except flashinfer_trace.TraceError as error:
        message = f"{path}: {error}"
        raise _fail(message) from error


def _require_top(top: int) -> None:
    if not 1 <= top <= _MAX_TOP:
        message = f"top must be between 1 and {_MAX_TOP}, got {top}"
        raise _fail(message)


def _flashinfer_version() -> str | None:
    try:
        return importlib.metadata.version("flashinfer-python")
    except importlib.metadata.PackageNotFoundError:
        return None


def register_tools(mcp: FastMCP, *, prefix: str = "") -> None:
    """Register timeline decoding and per-block summaries on ``mcp``.

    Every tool name starts with ``prefix``, so a server that offers these beside another
    profiler's tools (which has its own ``capabilities``) keeps the names apart.
    """

    @mcp.tool(name=f"{prefix}capabilities")
    def capabilities() -> dict[str, object]:
        """Describe accepted inputs, limits, and the FlashInfer package this interpreter sees."""
        return {
            "status": "ok",
            "inputs": {
                "flashinfer_buffer": (
                    "raw uint64 words written by FlashInfer's profiler macros, saved with "
                    "buffer.cpu().numpy().tofile(path); any suffix except .json"
                ),
                "chrome_json": "Chrome trace-event JSON (.json) with X or B/E events",
            },
            "max_input_bytes": _MAX_INPUT_BYTES,
            "timestamp_note": (
                "buffer timestamps are %globaltimer_lo nanoseconds; one capture must span "
                "under about 2.1 s"
            ),
            "flashinfer_python_in_this_interpreter": _flashinfer_version(),
        }

    @mcp.tool(name=f"{prefix}summary")
    def summary(
        path: str, event_names: list[str] | None = None, track_filter: str = "", top: int = 20
    ) -> dict[str, object]:
        """Per event: count, tracks, total, mean, p50, p95 and max duration.

        Args:
            path: A saved FlashInfer profiler buffer, or a Chrome trace ``.json``.
            event_names: Names for event indices 0..n-1 of a buffer, in the order the
                kernel's ``PROFILER_EVENT_*`` calls use them. Ignored for ``.json``.
            track_filter: Keep only tracks whose name contains this text.
            top: How many events to list, largest total first (at most 50).
        """
        _require_top(top)
        timeline = _load(path, event_names, track_filter)
        return {"path": path, **flashinfer_trace.summarize(timeline, top)}

    @mcp.tool(name=f"{prefix}tracks")
    def tracks(
        path: str, event_names: list[str] | None = None, track_filter: str = "", top: int = 20
    ) -> dict[str, object]:
        """Per block track: busy and idle time, the latest-finishing tracks first.

        Args:
            path: A saved FlashInfer profiler buffer, or a Chrome trace ``.json``.
            event_names: Names for a buffer's event indices. Ignored for ``.json``.
            track_filter: Keep only tracks whose name contains this text.
            top: How many tracks to list (at most 50).
        """
        _require_top(top)
        timeline = _load(path, event_names, track_filter)
        return {"path": path, **flashinfer_trace.tracks(timeline, top)}

    @mcp.tool(name=f"{prefix}event_skew")
    def event_skew(
        path: str,
        event: str,
        event_names: list[str] | None = None,
        track_filter: str = "",
        top: int = 10,
    ) -> dict[str, object]:
        """Align each occurrence of one event across blocks: wall, begin skew, imbalance.

        Also tallies which track finishes each occurrence last, the one a following
        grid-wide dependency waits for.

        Args:
            path: A saved FlashInfer profiler buffer, or a Chrome trace ``.json``.
            event: The event name to align.
            event_names: Names for a buffer's event indices. Ignored for ``.json``.
            track_filter: Keep only tracks whose name contains this text.
            top: How many worst occurrences and last finishers to list (at most 50).
        """
        _require_top(top)
        timeline = _load(path, event_names, track_filter)
        try:
            return {"path": path, **flashinfer_trace.event_skew(timeline, event, top)}
        except flashinfer_trace.TraceError as error:
            raise _fail(error) from error

    @mcp.tool(name=f"{prefix}track_window")
    def track_window(
        path: str,
        track: str,
        start_us: float = 0.0,
        end_us: float = 1e12,
        event_names: list[str] | None = None,
    ) -> dict[str, object]:
        """List one track's intervals inside a time window (at most 200).

        Args:
            path: A saved FlashInfer profiler buffer, or a Chrome trace ``.json``.
            track: An exact track name, as listed by ``tracks``.
            start_us: Window start, microseconds from the trace's first event.
            end_us: Window end, microseconds from the trace's first event.
            event_names: Names for a buffer's event indices. Ignored for ``.json``.
        """
        timeline = _load(path, event_names)
        try:
            return flashinfer_trace.window(timeline, track, start_us, end_us, _WINDOW_LIMIT)
        except flashinfer_trace.TraceError as error:
            raise _fail(error) from error

    @mcp.tool(name=f"{prefix}export_chrome_trace")
    def export_chrome_trace(
        path: str, output: str, event_names: list[str] | None = None
    ) -> dict[str, object]:
        """Write a buffer's timeline as Chrome trace JSON for ui.perfetto.dev.

        Args:
            path: A saved FlashInfer profiler buffer.
            output: A new ``.json`` path in an existing directory; never overwritten.
            event_names: Names for the buffer's event indices.
        """
        target = Path(output)
        if target.suffix != ".json" or target.exists() or not target.parent.is_dir():
            message = f"output must be a new .json file in an existing directory: {output}"
            raise _fail(message)
        timeline = _load(path, event_names)
        target.write_text(json.dumps(flashinfer_trace.to_chrome_trace(timeline)))
        return {
            "status": "ok",
            "output": str(target.resolve()),
            "tracks": len({item.track for item in timeline.intervals}),
            "intervals": len(timeline.intervals),
        }


def build_server(*, withheld: frozenset[str] = frozenset()) -> FastMCP:
    """Expose timeline decoding and per-block summaries.

    ``withheld`` names tools the run does not offer; none of these tools needs a GPU
    driver, so a run withholds none of them.
    """
    mcp = StrictFastMCP("vibesys-flashinfer-profiler")
    register_tools(mcp)
    for name in sorted(withheld):
        mcp.remove_tool(name)  # a name the server lacks raises: a stale list must not pass silently

    return mcp


def main(argv: list[str] | None = None) -> None:
    """Start the stdio MCP server."""
    parser = argparse.ArgumentParser(description="FlashInfer intra-kernel timeline MCP server")
    parser.parse_args(argv)
    build_server(withheld=capture_runtime.withheld_tools_from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
