"""Shared setup for the dynamic-loop scenarios."""

from __future__ import annotations

import pytest
from tests.support.container_credentials import set_container_cli_credentials


@pytest.fixture(autouse=True)
def _container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the agent container the credential its CLI would start with."""
    set_container_cli_credentials(monkeypatch)
