"""Stdio MCP server exposing kernel headroom-report analyses as MCP tools.

The orchestrator's profiler agent calls these tools to analyze a headroom
report JSON produced by the target's own capture entry point (conventionally
``benchmark/headroom_capture.sh``, or whatever ``OBJECTIVE.md`` documents).
Capture stays a shell command; see ``analyze_headroom.py`` for the report
schema this server understands.

Launch:

    python headroom_profiler/server.py
    # or
    uv run python headroom_profiler/server.py.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import Callable

_HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(_HERE))
# lint-waiver: LW-008016 [E402]; This standalone bundle adds a sibling module directory to sys.path before importing its modules.
import analyze_headroom  # noqa: E402


def _capture(fn: Callable[..., None], **kwargs: object) -> str:
    ns = types.SimpleNamespace(**kwargs)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(ns)
    except SystemExit as exc:  # _load() reports malformed reports via sys.exit
        return f"error: {exc}"
    out = buf.getvalue()
    return out or "(no output)"


def build_server() -> FastMCP:
    """Construct the FastMCP instance with headroom-report analysis tools."""
    mcp = FastMCP("vibesys-headroom-profiler")

    @mcp.tool()
    def waterfall(report: str) -> str:
        """Additive headroom waterfall (buckets) plus bucket definitions.

        Args:
            report: Path to the headroom report JSON.
        """
        return _capture(analyze_headroom.cmd_waterfall, report=report)

    @mcp.tool()
    def top(report: str, top: int = 15, klass: str | None = None) -> str:
        """Kernels ranked by recoverable ms/step, with source attribution.

        Args:
            report: Path to the headroom report JSON.
            top: Number of kernels to show (default 15).
            klass: Optional class filter (e.g. "movement", "quality").
        """
        return _capture(analyze_headroom.cmd_top, report=report, top=top, klass=klass)

    @mcp.tool()
    def kernel(report: str, name: str) -> str:
        """Full detail for every kernel whose name contains a substring.

        Args:
            report: Path to the headroom report JSON.
            name: Substring of the kernel name.
        """
        return _capture(analyze_headroom.cmd_kernel, report=report, name=name)

    @mcp.tool()
    def subgraphs(report: str) -> str:
        """Per-compiled-subgraph fusion view, when the report provides one.

        Args:
            report: Path to the headroom report JSON.
        """
        return _capture(analyze_headroom.cmd_subgraphs, report=report)

    @mcp.tool()
    def compare(old: str, new: str, top: int = 15) -> str:
        """Bucket and per-kernel deltas between two reports (old -> new).

        Args:
            old: Path to the earlier report JSON (e.g. a previous round's).
            new: Path to the later report JSON.
            top: Number of per-kernel deltas to show (default 15).
        """
        return _capture(analyze_headroom.cmd_compare, old=old, new=new, top=top)

    @mcp.tool()
    def summary(report: str, top: int = 10) -> str:
        """All-in-one: waterfall + top opportunities + caveats.

        Args:
            report: Path to the headroom report JSON.
            top: Number of kernels in the opportunities section (default 10).
        """
        return _capture(analyze_headroom.cmd_summary, report=report, top=top, klass=None)

    return mcp


def main(argv: list[str] | None = None) -> None:
    """Run the command-line entry point."""
    parser = argparse.ArgumentParser(
        prog="vibesys-headroom-mcp",
        description="Stdio MCP server exposing kernel headroom-report analyses.",
    )
    parser.parse_args(argv)
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
