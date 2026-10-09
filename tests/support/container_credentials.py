"""Credentials an agent container needs to start, for scenarios whose agents are scripted."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def set_container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export a synthetic credential for each CLI provider a scenario may name.

    Every environment runs the agent in a container, which refuses to start
    without the credential its CLI would use. The scenarios' agents are
    scripted and never present it to anything.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
