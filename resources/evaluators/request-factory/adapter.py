# lint-waiver: LW-008025 [INP001]; The upstream directory name contains a hyphen, so this adapter is executed as a standalone file and cannot be a Python package.
# ruff: noqa: INP001
"""Launch a task-owned Request Factory adapter with the trusted engine path."""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Inject the installed engine into one task-owned benchmark adapter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("script")
    parser.add_argument("script_arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    # lint-waiver: LW-008028 [S606]; Replacing the adapter process preserves direct argv execution and signal forwarding without a shell.
    os.execv(  # noqa: S606
        sys.executable,
        [
            sys.executable,
            arguments.script,
            "--request-factory-engine",
            arguments.engine,
            *arguments.script_arguments,
        ],
    )
    return 0  # pragma: no cover
