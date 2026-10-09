"""The crash sweeps run without coverage.

Each case re-runs a full simulation, which costs about twice as much under coverage, and
the sweeps were the slowest part of CI. Lines that only they reached have their own tests
(for example ``libs/vs-core/tests/test_recovery_lost_inspection.py``). pytest-cov skips a
test marked ``no_cover``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_HERE = __file__.rpartition("/")[0]


def _under(path: Path) -> bool:
    return str(path).startswith(_HERE + "/")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory (the hook sees the whole session's items)."""
    for item in items:
        if _under(item.path):
            item.add_marker(pytest.mark.no_cover)
