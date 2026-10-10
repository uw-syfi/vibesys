"""Keep only this runner's share of the tests when `CPU_CHECK_SHARD=I/N` is set."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cpu_check import shard

if TYPE_CHECKING:
    import pytest


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
