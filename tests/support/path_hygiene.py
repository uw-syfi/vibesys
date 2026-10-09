"""Keep ``sys.path`` edits made while collecting or running a test from outliving it.

The standalone profiler bundles under ``resources/`` insert their own directory
into ``sys.path`` at import time so their sibling modules resolve. Each of those
directories holds a ``server.py``. A test module that imports one at collection
leaves the directory on ``sys.path`` for the rest of the worker, and a later
``import server`` that misses ``sys.modules`` then binds a profiler script
instead of the ``src/server`` package, failing with ``'server' is not a
package``. Whether it misses depends on what the worker happened to import
first, so the failure appears in some shards and not others.

Restoring ``sys.path`` after each collection report and each test makes every
test see the path the session started with, whatever ran before it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, object, object]:
    """Undo ``sys.path`` edits that importing a test module made."""
    del collector
    snapshot = list(sys.path)
    try:
        return (yield)
    finally:
        sys.path[:] = snapshot


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    """Undo ``sys.path`` edits made by a test and its fixtures."""
    del item, nextitem
    snapshot = list(sys.path)
    try:
        return (yield)
    finally:
        sys.path[:] = snapshot
