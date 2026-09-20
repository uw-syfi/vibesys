"""Server-owned resources handed off from a live core run.

These dataclasses used to live in `vibesys.run.integration` as the shape of
the (now-deleted) `RunIntegration.attach_run` port. Core now hands off the
same facts through the type-erased `vibesys.run.integration.RunResourceHandoff`
via `RunSession.on_run_resources`; `RunIntegrationAdapter._handle_run_resources`
converts one into a `RunAttachment` here, which is what `ExperimentChatFactory`
and its callers still consume unchanged.
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
