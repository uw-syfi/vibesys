"""The core has no leaf left that raises "not implemented" for a reachable input."""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "vs_core"


def test_no_leaf_raises_kernel_not_implemented() -> None:
    """A leaf declines an input it does not support; it never raises the stub error."""
    assert SOURCE.is_dir()
    raising = [
        str(path.relative_to(SOURCE))
        for path in sorted(SOURCE.rglob("*.py"))
        if re.search(r"raise\s+KernelNotImplementedError", path.read_text())
    ]
    assert raising == []
