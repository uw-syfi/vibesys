"""Headless mode entry point for VibeSys runs.

Parses argv, dispatches through ``entrypoints.cli`` (argument parsing,
``RunRequest`` building, and validate/migrate subcommands), and renders any
``ConfigurationError`` to stderr with the diagnostic's exit code. Run as a
program, it prints any other uncaught error's traceback with its control
characters shown, since run errors carry text from agents and tools.
"""

from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING

from entrypoints.cli import _render_configuration_error, dispatch, start_process
from headless import printable
from vibesys.api import ConfigurationError

if TYPE_CHECKING:
    from types import TracebackType

__all__ = ["main", "print_uncaught"]


def main(argv: list[str] | None = None) -> None:
    """Run the headless command-line interface."""
    arguments = sys.argv[1:] if argv is None else argv
    start_process()
    try:
        dispatch(arguments)
    except ConfigurationError as exc:
        _render_configuration_error(exc)


def print_uncaught(
    kind: type[BaseException], error: BaseException, trace: TracebackType | None
) -> None:
    """Print an uncaught error's traceback as ``sys.excepthook`` would, control characters shown."""
    sys.stderr.write(printable("".join(traceback.format_exception(kind, error, trace))))


if __name__ == "__main__":
    sys.excepthook = print_uncaught
    main()
