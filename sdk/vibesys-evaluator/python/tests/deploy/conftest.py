from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .k8s_world import World, make_world

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)
