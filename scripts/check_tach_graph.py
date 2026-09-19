#!/usr/bin/env python3
"""Keep the Mermaid module graph in docs/contributing/architecture.md current.

`tach show --mermaid` renders the module graph declared in `tach.toml`. This
script embeds two views between marker comments in the architecture doc:

    1. the full graph, and
    2. the core strongly connected component (the known cycle), filtered from
       the full graph so the cycle is legible.

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

DOC = Path("docs/contributing/architecture.md")
START = "<!-- tach-graph:start -->"
END = "<!-- tach-graph:end -->"

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


def render_block(edges: list[tuple[str, str]]) -> str:
    """Build the marked region: the full graph plus the core-cycle view."""
    core = [e for e in edges if e[0] in CORE and e[1] in CORE]
    return "\n".join(
        [
            START,
            "## Full graph",
            "",
            "```mermaid",
            mermaid(edges),
            "```",
            "",
            "## Core cycle",
            "",
            "Edges among the modules of the known strongly connected core.",
            "",
            "```mermaid",
            mermaid(core),
            "```",
            END,
        ]
    )


def splice(text: str, block: str) -> str:
    """Replace the marked region of `text` with `block`."""
    start = text.find(START)
    end = text.find(END)
    if start == -1 or end == -1 or end < start:
        msg = f"{DOC} is missing the {START} / {END} markers"
        raise ValueError(msg)
    return text[:start] + block + text[end + len(END) :]


def main() -> int:
    """Run the check or regenerate the doc block."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate the block")
    mode.add_argument("--check", action="store_true", help="fail if stale (default)")
    parser.add_argument("--root", type=Path, default=Path())
    args = parser.parse_args()

    doc = args.root / DOC
    try:
        current = doc.read_text()
        expected = splice(current, render_block(tach_edges()))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    if args.write:
        if expected != current:
            doc.write_text(expected)
            print(f"updated {DOC}")
        return EXIT_OK
    if expected != current:
        print(
            f"{DOC} tach graph is stale. Run: uv run python scripts/check_tach_graph.py --write",
            file=sys.stderr,
        )
        return EXIT_STALE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
