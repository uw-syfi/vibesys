"""Keep only this runner's share of the tests when `CPU_CHECK_SHARD=I/N` is set."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from cpu_check import shard

if TYPE_CHECKING:
    import pytest


def _repository_root() -> str | None:
    """The repository that owns the hang-dump plugin, when this tree sits inside it."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "pytest_hang_dump.py").is_file():
            return str(parent)
    return None


# This directory is its own pytest root, so the repository conftest does not load.
# VIBESYS_PYTEST_DUMP_AT is unset locally and the plugin stays out.
if os.environ.get("VIBESYS_PYTEST_DUMP_AT"):
    _root = _repository_root()
    if _root is not None and _root not in sys.path:
        sys.path.insert(0, _root)
    if _root is not None:
        pytest_plugins = ["scripts.pytest_hang_dump"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    spec = os.environ.get(shard.SHARD_ENV)
    if not spec:
        return
    index, total = shard.parse_shard(spec)
    placed = shard.assign(
        [(item.nodeid, getattr(item, "originalname", item.name)) for item in items],
        shard.load_durations(),
        total,
    )
    items[:] = [item for item in items if placed[item.nodeid] == index - 1]
