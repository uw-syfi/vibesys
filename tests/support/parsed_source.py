"""One parse per source text for the tests that scan the whole repository.

Architecture and quality rules each walk the syntax trees of thousands of files. Parsing
dominates their cost, and each rule used to parse every file for itself. Parsing through
this module shares one tree per distinct source text within a worker, so the rules add
only their own walks. Static-scan modules carry ``xdist_group("static_scan")`` so one
worker holds the shared trees.

The trees are shared: a scan reads them and never edits them.
"""

from __future__ import annotations

import ast
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Marker group for modules that scan the repository's syntax trees.
STATIC_SCAN_GROUP = "static_scan"


@cache
def parse_source(source: str, filename: str = "<unknown>") -> ast.Module:
    """The syntax tree of ``source``, parsed once per ``(source, filename)``."""
    return ast.parse(source, filename=filename)


def parse_file(path: Path) -> ast.Module:
    """The syntax tree of the file at ``path`` (read as UTF-8)."""
    return parse_source(path.read_text(encoding="utf-8"), str(path))


@cache
def python_files(root: Path) -> tuple[Path, ...]:
    """Every ``*.py`` under ``root``, sorted; the directory walk is shared too."""
    return tuple(sorted(root.rglob("*.py")))


@cache
def _nodes(tree: ast.AST) -> tuple[ast.AST, ...]:
    return tuple(ast.walk(tree))


def walk_nodes(tree: ast.AST) -> tuple[ast.AST, ...]:
    """``ast.walk(tree)`` as a tuple, computed once per node.

    A scan walks a tree in Python, which is the traced, slow part under coverage; the rules
    that share a parsed file also share its walk.
    """
    return _nodes(tree)
