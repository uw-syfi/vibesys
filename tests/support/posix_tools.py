"""Command-line tools that reject GNU-only flags, so Linux CI catches macOS breakage.

Scripts that the Fake Slurm connector runs locally must work with the BSD
coreutils on a macOS dev machine. Linux CI has GNU coreutils, which accept more
flags, so a GNU-only flag passes CI and fails on a Mac. Putting these shims first
on ``PATH`` makes the stricter contract hold everywhere.

Every shim rejects GNU long options (``--flag``) and the listed short flags
that BSD tools lack or spell differently, then runs the real tool. A bare
``--`` ends option checking, as in the tools themselves. ``timeout`` is not
shimmed: it is not POSIX, and Slurm batch scripts that use it run on Linux nodes.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# tool -> short flags that are GNU-only or mean something else under BSD.
GNU_ONLY_SHORT_FLAGS: dict[str, tuple[str, ...]] = {
    "mv": ("-T", "-t"),
    "cp": ("-T", "-t"),
    "sed": ("-i", "-r"),
    "date": ("-d", "-I"),
    "stat": ("-c",),
    "readlink": ("-f", "-e", "-m"),
}


def install_posix_tool_shims(directory: Path) -> Path:
    """Create the shims in ``directory`` (made if absent) and return it.

    Real tools are resolved now, so the shims keep working once ``directory``
    leads ``PATH``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for tool, flags in GNU_ONLY_SHORT_FLAGS.items():
        real = shutil.which(tool)
        if real is None:
            continue
        rejected = "|".join((*flags, "--*"))
        shim = directory / tool
        shim.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            "    --) break ;;\n"
            f'    {rejected}) echo "{tool}: GNU-only option $arg is not portable" >&2; exit 64 ;;\n'
            "  esac\n"
            "done\n"
            f'exec {real} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return directory
