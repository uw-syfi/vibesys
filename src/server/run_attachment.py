"""Server-owned resources describing a live core run.

`RunIntegrationAdapter.handle_run_ready` builds a `RunAttachment` from the
public readiness contract; `ExperimentChatFactory` consumes it to attach an
experiment-chat surface to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.api import AgentSelection

if TYPE_CHECKING:
    from vs_project.api import Project


@dataclass(frozen=True, slots=True)
class RunAttachment:
    """Core resources exposed to optional application-owned run surfaces.

    Auxiliary-agent construction remains on ``RunSession``.  This server value
    retains only state identity and the defaults shown in chat settings.
    """

    project: Project
    run_id: str
    agent_defaults: AgentSelection


__all__ = ["AgentSelection", "RunAttachment"]
