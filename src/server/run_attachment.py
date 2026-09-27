"""Server-owned resources describing a live core run.

`RunIntegrationAdapter.handle_run_ready` builds a `RunAttachment` from the
public readiness contract; `ExperimentChatFactory` consumes it to attach an
experiment-chat surface to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import AgentDriver, AuxiliaryAgentDriver


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """Resolved agent choice owned by an optional server surface."""

    driver: AgentDriver
    provider: str
    model: str
    role_models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunAttachment:
    """Core resources exposed to optional application-owned run surfaces.

    Auxiliary-agent construction remains on ``RunSession``.  This server value
    retains only state identity and the defaults shown in chat settings.
    """

    chat_state_dir: Path
    agent_defaults: AgentSelection
    agent_drivers: tuple[AuxiliaryAgentDriver, ...]


__all__ = ["AgentSelection", "RunAttachment"]
