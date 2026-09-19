#!/usr/bin/env python3
"""Keep the Mermaid module graph files under docs/figures/ current.

`tach show --mermaid` renders the module graph declared in `tach.toml`. This
script writes two whole generated files:

    1. docs/figures/tach-module-graph.mmd, the full graph, and
    2. docs/figures/tach-module-graph-core.mmd, the core strongly connected
       component (the known cycle), filtered from the full graph.

Tach's edge order is not guaranteed stable, so edges are sorted. Only the local
Mermaid output is used; never `tach show --web`, which uploads the graph.

Usage:
    uv run python scripts/check_tach_graph.py           # same as --check
    uv run python scripts/check_tach_graph.py --check   # fail if block is stale
    uv run python scripts/check_tach_graph.py --write   # regenerate the block
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FULL = Path("docs/figures/tach-module-graph.mmd")
CORE_VIEW = Path("docs/figures/tach-module-graph-core.mmd")

# Modules of the known core cycle, using the exact names from tach.toml.
CORE = frozenset(
    {
        "vibesys",
        "vibesys.agents",
        "vibesys.run",
        "vibesys.render",
        "vibesys.sandbox",
        "vibesys.backends",
        "vibesys.skypilot",
        "vibesys.domains",
        "vibesys.prompts",
        "vibesys.evaluators",
    }
)

EDGE_TOKENS = 3
EXIT_OK = 0
EXIT_STALE = 1
EXIT_TOOL_ERROR = 2


def tach_edges() -> list[tuple[str, str]]:
    """Return the sorted `(src, dst)` edges from `tach show --mermaid`."""
    result = subprocess.run(
        ["uv", "run", "tach", "show", "--mermaid", "-o", "-"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    edges = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == EDGE_TOKENS and parts[1] == "-->":
            edges.add((parts[0], parts[2]))
    return sorted(edges)


def mermaid(edges: list[tuple[str, str]]) -> str:
    """Render edges as a Mermaid top-down graph."""
    lines = ["graph TD"] + [f"    {src} --> {dst}" for src, dst in edges]
    return "\n".join(lines)


def render_files(edges: list[tuple[str, str]]) -> dict[Path, str]:
    """Return the generated files: the full graph and the core-cycle view."""
    core = [e for e in edges if e[0] in CORE and e[1] in CORE]
    return {FULL: mermaid(edges) + "\n", CORE_VIEW: mermaid(core) + "\n"}


def main() -> int:
    """Run the check or regenerate the doc block."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate the block")
    mode.add_argument("--check", action="store_true", help="fail if stale (default)")
    parser.add_argument("--root", type=Path, default=Path())
    args = parser.parse_args()

    try:
        expected = render_files(tach_edges())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    stale = []
    for rel, text in expected.items():
        path = args.root / rel
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"updated {rel}")
        else:
            stale.append(str(rel))
    if stale:
        print(
            f"stale tach graph: {', '.join(stale)}. "
            "Run: uv run python scripts/check_tach_graph.py --write",
            file=sys.stderr,
        )
        return EXIT_STALE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
