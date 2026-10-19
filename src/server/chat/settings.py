"""The run's own agent selection, which a chat thread without a harness choice runs.

The catalogue a thread's harness is chosen from is the helper's ``harnesses.list``, the one
New run draws on; the run server holds no list of its own. It holds only the providers this run
supports, which refuse a choice the run cannot start (``ChatRunSettings.agent_providers``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatRunSettings:
    """The run's own provider and model, and the providers its chat can start a thread on."""

    provider: str
    model: str
    agent_providers: tuple[str, ...]
