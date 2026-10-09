"""Fixtures shared by the end-to-end scenarios."""

from __future__ import annotations

import pytest
from tests.support.container_credentials import set_container_cli_credentials


@pytest.fixture
def container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give a scripted scenario's agent container the credential its CLI would start with."""
    set_container_cli_credentials(monkeypatch)
