"""Server-owned resources describing a live core run.

`RunIntegrationAdapter._handle_run_resources` builds a `RunAttachment` from the
run's resource handoff; `ExperimentChatFactory` and its callers consume it to
attach an experiment-chat surface to the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

from vs_agent import AgentSelection  # noqa: TC001  # tracked: #288

if TYPE_CHECKING:
    from vs_project import Project


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
