"""Server-owned resources describing a live core run.

`RunIntegrationAdapter.handle_run_resources` builds a `RunAttachment` from the
run's resource handoff; `ExperimentChatFactory` and its callers consume it to
attach an experiment-chat surface to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_agent.api import AgentSelection

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import Project


@dataclass(frozen=True, slots=True)
class RunAttachment:
    """Core resources exposed to optional application-owned run surfaces.

    Chat now builds its own agent environment through
    `vibesys.api.RunSession.open_agent_environment`, so this no longer carries
    the run's sandbox/config internals; it is just the run identity plus the
    default agent selection the experiment-chat surface needs.
    """

    project: Project
    run_id: str
    workspace: Path
    log_dir: Path
    agent_backend: str
    agent_defaults: AgentSelection


__all__ = ["AgentSelection", "RunAttachment"]
