"""One MCP server offering both NCU report tools and FlashInfer timeline tools.

The NCU and FlashInfer support packages are staged beside this one (the profiler
definition lists both as extra support kinds). This server registers each package's
tools on one instance: NCU's under their own names, the timeline tools with a
``timeline_`` prefix, since both packages have a ``capabilities`` tool. Like its parts,
it only reads saved artifacts and never runs candidate code.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    from mcp.server.fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent
TIMELINE_PREFIX = "timeline_"


def _sibling_server(kind: str) -> ModuleType:
    """Load a sibling package's ``server.py``: ``<kind>/`` in a checkout, ``<kind>_profiler/`` staged."""
    for directory in (_HERE.parent / kind, _HERE.parent / f"{kind}_profiler"):
        path = directory / "server.py"
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location(f"_ncu_flashinfer_{kind}_server", path)
        if spec is None or spec.loader is None:
            break
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    message = f"the {kind} support package is not staged beside {_HERE}"
    raise FileNotFoundError(message)


_ncu = _sibling_server("ncu")
_flashinfer = _sibling_server("flashinfer")
# The NCU sibling loads the shared runtime staged beside every profiler package.
capture_runtime = _ncu.capture_runtime
StrictFastMCP = _ncu.StrictFastMCP


def build_server(*, withheld: frozenset[str] = frozenset()) -> FastMCP:
    """Expose NCU report inspection and FlashInfer timeline analysis together.

    ``withheld`` names tools the run does not offer (NCU's ``query_metrics`` needs a GPU
    driver in the editor).
    """
    mcp = StrictFastMCP("vibesys-ncu-flashinfer-profiler")
    _ncu.register_tools(mcp)
    _flashinfer.register_tools(mcp, prefix=TIMELINE_PREFIX)
    for name in sorted(withheld):
        mcp.remove_tool(name)  # a name the server lacks raises: a stale list must not pass silently

    return mcp


def main(argv: list[str] | None = None) -> None:
    """Start the stdio MCP server."""
    parser = argparse.ArgumentParser(description="NCU and FlashInfer timeline MCP server")
    parser.parse_args(argv)
    build_server(withheld=capture_runtime.withheld_tools_from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
