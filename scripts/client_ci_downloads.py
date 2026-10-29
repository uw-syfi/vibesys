#!/usr/bin/env python3
"""Facts shared by the client setup action and its quality test.

Electron's postinstall downloads a binary the pnpm store does not contain.
``electron_version`` reads the one resolved version from a pnpm lockfile so
the cache key can name it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_ELECTRON_PACKAGE = re.compile(r"(?m)^  electron@(\d+\.\d+\.\d+(?:[-+][^\s:]+)?):\s*$")
EXIT_OK = 0
EXIT_LOCKFILE = 1


class ElectronVersionError(ValueError):
    """The lockfile does not pin exactly one Electron version."""

    @classmethod
    def wrong_pin_count(cls, found: set[str]) -> ElectronVersionError:
        """Build the error listing the versions the lockfile pinned.

        Args:
            found: The distinct ``electron@`` versions in the lockfile.

        Returns:
            The error naming those versions, or ``none`` when the set is empty.
        """
        listed = ", ".join(sorted(found)) if found else "none"
        return cls(f"pnpm-lock.yaml must pin exactly one electron version, found {listed}")


def electron_version(lockfile: str) -> str:
    """Return the single ``electron@<version>`` pin in a pnpm lockfile.

    Args:
        lockfile: The lockfile text.

    Returns:
        The resolved Electron version.

    Raises:
        ElectronVersionError: The lockfile pins zero versions or more than one.
    """
    found = set(_ELECTRON_PACKAGE.findall(lockfile))
    if len(found) != 1:
        raise ElectronVersionError.wrong_pin_count(found)
    return next(iter(found))


def main(argv: Sequence[str] | None = None) -> int:
    """Print the Electron version pinned by a pnpm lockfile path.

    Args:
        argv: Arguments after the program name. ``None`` reads the process args.

    Returns:
        ``EXIT_OK`` when one version is printed, otherwise ``EXIT_LOCKFILE``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lockfile")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        text = _read_lockfile(args.lockfile)
        version = electron_version(text)
    except (ElectronVersionError, OSError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_LOCKFILE
    print(version)
    return EXIT_OK


def _read_lockfile(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        message = f"cannot read {path}: {exc.strerror or exc}"
        raise OSError(message) from exc


if __name__ == "__main__":
    raise SystemExit(main())
