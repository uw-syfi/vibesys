"""The import roots a child pytest needs to load the repository's conftest."""

from __future__ import annotations

import ast
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


#: Arguments that start a pytest with only the plugins a test passes with ``-p``.
#: Every installed plugin otherwise loads in the child (httpx through respx, Hypothesis,
#: coverage), which costs more than the check the child runs; none of them is under test.
BARE_PYTEST = (
    "-p",
    "no:cacheprovider",
    "-p",
    "no:tach",
    "-p",
    "no:asyncio",
    "-p",
    "no:anyio",
    "-p",
    "no:respx",
    "-p",
    "no:hypothesispytest",
    "-p",
    "no:cov",
)


def root_conftest_plugins(repository: Path) -> tuple[str, ...]:
    """The plugin modules the repository's root ``conftest.py`` declares in ``pytest_plugins``.

    Read from the source, not by importing the conftest: a child that loads the plugin under
    test alone is only a faithful stand-in for the suite if the suite loads that plugin.
    """
    tree = ast.parse((repository / "conftest.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins"
            for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    return ()
