"""Headless mode entry point for VibeSys runs.

Parses argv, dispatches through ``entrypoints.cli`` (argument parsing,
``RunRequest`` building, and validate/migrate subcommands), and renders any
``ConfigurationError`` to stderr with the diagnostic's exit code.
"""

from __future__ import annotations

import sys

from entrypoints.cli import _render_configuration_error, dispatch
from vibesys.api import ConfigurationError

__all__ = ["main"]


def main(argv: list[str] | None = None) -> None:
    """Run the headless command-line interface."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        dispatch(arguments)
    except ConfigurationError as exc:
        _render_configuration_error(exc)


if __name__ == "__main__":
    main()
