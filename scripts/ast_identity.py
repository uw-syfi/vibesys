"""One interpreter-independent text identity for an AST node.

Baselines that record ``ast.dump`` output must read the same under every supported
Python. Since 3.13 ``ast.dump`` leaves out empty lists and ``None`` fields unless asked to
show them, so the same source dumps differently under 3.12 and 3.14. Pinning
``show_empty=True`` keeps the 3.12 spelling everywhere.
"""

from __future__ import annotations

import ast
import sys


def node_identity(node: ast.AST) -> str:
    """``ast.dump`` without positions, spelled as Python 3.12 spells it."""
    if sys.version_info >= (3, 13):
        return ast.dump(node, include_attributes=False, show_empty=True)
    return ast.dump(node, include_attributes=False)
