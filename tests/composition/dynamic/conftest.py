"""Shared setup for the dynamic-loop scenarios."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give the agent container the credential its CLI would start with.

    Every environment runs the agent in a container, which refuses to start
    without one; the scenarios' agents are scripted and never use it.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    return
