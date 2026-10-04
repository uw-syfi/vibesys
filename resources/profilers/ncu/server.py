"""MCP tools for bounded inspection of Nsight Compute reports.

Capture is a shell command run in the workload's execution environment. This
server reads retained .ncu-rep artifacts and never profiles a scored benchmark.
"""

from __future__ import annotations

import argparse
import functools
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

# The same common runtime is staged beside each standalone profiler bundle.
for _common_name in ("_common", "profilers_common"):
    _common_path = Path(__file__).resolve().parent.parent / _common_name
    if (_common_path / "capture_runtime.py").is_file():
        sys.path.insert(0, str(_common_path))
        break
capture_runtime = importlib.import_module("capture_runtime")

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


def _invoke(
    *args: str,
    output_limit: int = _OUTPUT_LIMIT,
    runner: capture_runtime.CommandRunner = subprocess.run,
) -> dict[str, object]:
    executable = _ncu_executable()
    if executable is None:
        diagnostic = "Nsight Compute executable unavailable; set NCU_PATH to its executable or put ncu on PATH."
        raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
    try:
        result = runner(
            [executable, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        diagnostic = "Nsight Compute command exceeded 60 seconds."
        raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic) from error
    except OSError as error:
        raise capture_runtime.CaptureFailedError.analysis_failed(error) from error
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        diagnostic = f"Nsight Compute exited {result.returncode}: {output[:output_limit]}"
        raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
    truncated = len(output) > output_limit
    if truncated:
        output = output[:output_limit]
    return {
        "status": "ok",
        "exit_code": result.returncode,
        "output": output,
        "truncated": truncated,
    }


def build_server(*, runner: capture_runtime.CommandRunner = subprocess.run) -> FastMCP:
    """Expose NCU availability and bounded report inspection."""
    mcp = FastMCP("vibesys-ncu-profiler")
    invoke = functools.partial(_invoke, runner=runner)

    @mcp.tool()
    def capabilities() -> dict[str, object]:
        """Check the configured Nsight Compute executable and its version."""
        return invoke("--version")

    @mcp.tool()
    def list_sections_and_sets() -> dict[str, object]:
        """List Nsight Compute metric sections and preset collection sets."""
        sections = invoke("--list-sections")
        if sections["status"] != "ok":
            return sections
        sets = invoke("--list-sets")
        return {"sections": sections, "sets": sets}

    @mcp.tool()
    def query_metrics(substring: str = "") -> dict[str, object]:
        """Find supported metric names by substring (up to 100 results)."""
        result = invoke("--query-metrics", output_limit=2_000_000)
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
            diagnostic = f"NCU report does not exist or is not a .ncu-rep file: {path}"
            raise capture_runtime.CaptureFailedError.analysis_failed(diagnostic)
        result = invoke(
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
