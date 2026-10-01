"""MCP tools for bounded inspection of Nsight Compute reports.

Capture is a shell command run in the workload's execution environment. This
server reads retained .ncu-rep artifacts and never profiles a scored benchmark.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

_OUTPUT_LIMIT = 16_000
_LINE_LIMIT = 100


def _ncu_executable() -> str | None:
    configured = os.environ.get("NCU_PATH")
    if configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which("ncu")


def _invoke(*args: str, output_limit: int = _OUTPUT_LIMIT) -> dict[str, object]:
    executable = _ncu_executable()
    if executable is None:
        return {
            "status": "unavailable",
            "diagnostic": "Nsight Compute executable unavailable; set NCU_PATH to its executable or put ncu on PATH.",
        }
    try:
        result = subprocess.run(  # noqa: S603
            [executable, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "diagnostic": "Nsight Compute command exceeded 60 seconds."}
    except OSError as error:
        return {"status": "error", "diagnostic": str(error)}
    output = (result.stdout + result.stderr).strip()
    truncated = len(output) > output_limit
    if truncated:
        output = output[:output_limit]
    return {
        "status": "ok" if result.returncode == 0 else "error",
        "exit_code": result.returncode,
        "output": output,
        "truncated": truncated,
    }


def build_server() -> FastMCP:
    """Expose NCU availability and bounded report inspection."""
    mcp = FastMCP("vibesys-ncu-profiler")

    @mcp.tool()
    def capabilities() -> dict[str, object]:
        """Check the configured Nsight Compute executable and its version."""
        return _invoke("--version")

    @mcp.tool()
    def list_sections_and_sets() -> dict[str, object]:
        """List Nsight Compute metric sections and preset collection sets."""
        sections = _invoke("--list-sections")
        if sections["status"] != "ok":
            return sections
        sets = _invoke("--list-sets")
        return {"sections": sections, "sets": sets}

    @mcp.tool()
    def query_metrics(substring: str = "") -> dict[str, object]:
        """Find supported metric names by substring (up to 100 results)."""
        result = _invoke("--query-metrics", output_limit=2_000_000)
        if result["status"] != "ok":
            return result
        lines = str(result["output"]).splitlines()
        matches = [line for line in lines if substring.lower() in line.lower()]
        return {
            "status": "ok",
            "metrics": matches[:_LINE_LIMIT],
            "total_matches": len(matches),
            "query_truncated": result["truncated"],
        }

    @mcp.tool()
    def read_report(
        path: str,
        page: Literal["details", "raw", "source", "session"] = "details",
        contains: str = "",
    ) -> dict[str, object]:
        """Read a retained .ncu-rep artifact with a bounded textual result.

        Args:
            path: Path to an existing .ncu-rep file.
            page: Details, raw metrics, annotated source, or session metadata.
            contains: Optional case-insensitive line filter for large reports.
        """
        artifact = Path(path)
        if not artifact.is_file() or artifact.suffix != ".ncu-rep":
            return {
                "status": "error",
                "diagnostic": f"NCU report does not exist or is not a .ncu-rep file: {path}",
            }
        result = _invoke(
            "--import",
            str(artifact.resolve()),
            "--page",
            page,
            "--csv",
            output_limit=2_000_000 if contains else _OUTPUT_LIMIT,
        )
        if contains and result["status"] == "ok":
            lines = str(result["output"]).splitlines()
            matches = [line for line in lines if contains.lower() in line.lower()]
            filtered = "\n".join(matches[:_LINE_LIMIT])
            result["output"] = filtered[:_OUTPUT_LIMIT]
            result["matching_lines"] = len(matches)
            result["truncated"] = (
                bool(result["truncated"])
                or len(matches) > _LINE_LIMIT
                or len(filtered) > _OUTPUT_LIMIT
            )
        return {"report": str(artifact.resolve()), "page": page, **result}

    return mcp


def main(argv: list[str] | None = None) -> None:
    """Start the stdio MCP server."""
    parser = argparse.ArgumentParser(description="Nsight Compute report MCP server")
    parser.parse_args(argv)
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
