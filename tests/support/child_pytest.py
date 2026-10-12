"""The import roots a child pytest needs to load the repository's conftest."""

from __future__ import annotations

import os
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def child_pythonpath(repository: Path) -> str:
    """A ``PYTHONPATH`` with the repository's pytest import roots, then the current one.

    A child pytest that runs a copy of the root conftest in a scratch directory does not read the
    repository's pytest settings, so it would not find the plugins the conftest loads. This gives
    it the same roots, read from ``[tool.pytest.ini_options] pythonpath``.
    """
    settings = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    roots = settings["tool"]["pytest"]["ini_options"]["pythonpath"]
    return os.pathsep.join(
        (*(str(repository / root) for root in roots), os.environ.get("PYTHONPATH", ""))
    )
