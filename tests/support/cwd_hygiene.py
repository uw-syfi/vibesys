"""Keep a test's working-directory change from outliving it.

Many tests read repository files through paths relative to the working
directory (``Path("examples/...")``). A test that changes the directory without
restoring it (for example by calling an entry point that ``os.chdir``s) leaves
every later test in the same worker reading from the wrong directory. Which
tests share a worker depends on xdist's scheduling, so the failure shows up in
some runs and not in others.

Restoring the directory after each test makes every test start where the
session started, whatever ran before it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    """Undo a working-directory change made by a test and its fixtures."""
    del item, nextitem
    start = Path.cwd()
    try:
        return (yield)
    finally:
        os.chdir(start)
