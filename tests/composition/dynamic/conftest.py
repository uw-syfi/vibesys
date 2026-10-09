"""Shared setup for the dynamic-loop scenarios."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the agent container the credential its CLI would start with.

    Every environment runs the agent in a container, which refuses to start
    without one; the scenarios' agents are scripted and never use it.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
