"""Test configuration: Hypothesis runs a fixed example set, and a failure prints how to replay it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings

from tests import helpers as h

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

settings.register_profile(
    "sidecar",
    max_examples=200,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("sidecar")


@pytest.fixture(autouse=True)
def plugin_root(tmp_path: Path) -> Iterator[Path]:
    """Every launch a test builds loads a plugin written under this test's own directory."""
    root = tmp_path / "plugin-world"
    h.use_plugin_root(root)
    yield root
    h.use_plugin_root(None)
