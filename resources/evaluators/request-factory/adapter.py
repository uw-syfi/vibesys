# lint-waiver: LW-008025 [INP001]; The upstream directory name contains a hyphen, so this adapter is executed as a standalone file and cannot be a Python package.
# ruff: noqa: INP001
"""Launch a task-owned Request Factory adapter with the trusted engine path."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_FORWARD_PREFIX_LENGTH = 3
_USAGE = "usage: adapter.py --engine <path> -- <script> [arguments ...]"


def main(argv: Sequence[str] | None = None) -> int:
    """Inject the installed engine into one task-owned benchmark adapter."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        len(arguments) <= _FORWARD_PREFIX_LENGTH
        or arguments[0] != "--engine"
        or arguments[2] != "--"
    ):
        raise ValueError(_USAGE)
    engine = arguments[1]
    script = arguments[3]
    script_arguments = arguments[4:]
    # lint-waiver: LW-008028 [S606]; Replacing the adapter process preserves direct argv execution and signal forwarding without a shell.
    os.execv(  # noqa: S606
        sys.executable,
        [
            sys.executable,
            script,
            "--request-factory-engine",
            engine,
            *script_arguments,
        ],
    )
    return 0  # pragma: no cover
