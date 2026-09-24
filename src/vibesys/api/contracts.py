"""Public contracts for `vibesys.api`: DTOs, enums, and the event sink.

No behavior lives here. Types that already exist in vibesys core are
re-exported instead of duplicated. `WorkspaceHandle` is deliberately not a
type defined here: a run's workspace is expressed as `vs_sandbox.HostResource`
(see `vibesys.api.session.RunWorkspace`) to avoid a lib -> core cycle.
"""

from __future__ import annotations

import warnings
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from vibesys.api.run_request import OrchestrationRunRequest, ResumeRef, RunRequestLike
from vibesys.config import Config
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEvent, EventStatus
from vs_agent.api import MCPServerSpec
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api._orchestrations.legacy_request import LoopKind, RunRequest
    from vibesys.skills import SkillSelection
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox

# Objective/MetricSpace live in vibesys.loops.metrics because the
# metric-comparison logic they carry is loop code.
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.schemas import CandidateDisposition, PerfDeltaReason

__all__ = [
    "AgentEnvironment",
    "CandidateDisposition",
    "Config",
    "ConfigurationDiagnostic",
    "ConfigurationError",
    "CoreEvent",
    "EventSink",
    "EventStatus",
    "LoopKind",
    "MCPServerSpec",
    "MetricSpace",
    "Objective",
    "OrchestrationDescriptor",
    "OrchestrationRunRequest",
    "PerfDeltaReason",
    "ResumeRef",
    "RunRequest",
    "RunRequestLike",
    "RunResult",
    "RunStatus",
    "RunView",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve deprecated built-in request names on explicit access."""
    if name not in {"LoopKind", "RunRequest"}:
        raise AttributeError(name)
    warnings.warn(
        f"vibesys.api.contracts.{name} is deprecated for new policies; use OrchestrationRunRequest",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(import_module("vibesys.api._orchestrations.legacy_request"), name)


class RunResult(BaseModel):
    """Terminal outcome of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    succeeded: bool


class RunStatus(StrEnum):
    """Lifecycle status `vibesys.api` can report for a run.

    Deliberately a narrower, separately-owned vocabulary from `EventStatus`
    (not a re-export): `RunQuery.view` on a live session reports `ACTIVE`
    while its loop is running, then `COMPLETED`/`FAILED` from the same
    `RUN_FINISHED`/`RUN_FAILED` transition `create_session` already emits as
    `EventStatus.COMPLETED`/`EventStatus.FAILED` (see `session.py`). `RunStore`
    projects a run from its durable files alone, which carry no lifecycle
    field, so it always reports `UNKNOWN` rather than guessing whether the
    process that wrote them is still attached.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class RunView(BaseModel):
    """Read-only run identity, lifecycle, and a policy-owned JSON projection.

    The selected orchestration owns the payload schema. An absent projection
    means that the policy has no persisted read model or is unavailable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    status: RunStatus
    projection: dict[str, JsonValue] | None = None


class AgentEnvironment(Protocol):
    """A live agent-construction environment opened for one run.

    Returned by `vibesys.api.session.RunAgentHost.open_agent_environment`.
    Carries exactly what `server.chat.factory.build_chat_agent` needs to build
    a sibling agent over the run's workspace: the construction inputs (`config`,
    `skill_selection`,
    `skill_source_dirs`, `project_path_policy`, `host_resources`), the opened
    sandbox's shape (`backends`, `use_docker`, `isolated`), its path
    translation (`agent_path`), and its lifetime (`close`).

    Every data member is a read-only property, not a plain attribute: no
    caller writes any of them, and this protocol's sole real implementation
    (`vibesys.api.session._OpenedAgentEnvironment`) is a frozen dataclass, so
    a plain attribute (implicitly read-write) would make it structurally
    incompatible with this protocol.
    """

    @property
    def config(self) -> Config:
        """This environment's agent configuration."""
        ...

    @property
    def skill_selection(self) -> SkillSelection:
        """The skill-pruning policy a sibling agent should apply while copying."""
        ...

    @property
    def skill_source_dirs(self) -> tuple[Path, ...]:
        """Directories a sibling agent should load skills from."""
        ...

    @property
    def project_path_policy(self) -> ProjectPathPolicy:
        """The path policy governing this environment's project access."""
        ...

    @property
    def host_resources(self) -> tuple[HostResource, ...]:
        """Host resources mounted into this environment."""
        ...

    @property
    def backends(self) -> dict[str, Sandbox] | None:
        """Sandbox handles for this environment's execution surfaces, if sandboxed."""
        ...

    @property
    def use_docker(self) -> bool:
        """Whether this environment's CLI runs sandboxed under Docker."""
        ...

    @property
    def isolated(self) -> bool:
        """Whether this environment runs with an isolated (non-host-mounted) workspace."""
        ...

    def agent_path(self, host: Path) -> str:
        """Map a host path to its path inside this environment's sandbox."""
        ...

    def investigation_tools(self) -> tuple[MCPServerSpec, ...]:
        """Return the read-only MCP tool servers for investigating this run's history.

        Each spec launches a `vibesys.api.chat_tools_server` subprocess scoped
        to this environment's run, exposing its read-model
        (`vibesys.api.RunStore`) as MCP tools instead of materializing files
        into the sandbox for a shell to `rg`/`tail`.
        """
        ...

    def close(self) -> None:
        """Release the opened environment session."""
        ...


class EventSink(Protocol):
    """Receives the semantic core event stream for one run.

    Matches the duck-typed subscriber shape already used by
    `vibesys.render.sink.EventHandler` and
    `vibesys.run.event_journal.EventSubscriber`
    (`Callable[[CoreEvent], None]`): any plain function or bound method with
    this signature satisfies it, including a headless renderer's bound
    `.handle` method.
    """

    def __call__(self, event: CoreEvent) -> None:
        """Handle one emitted core event."""
        ...
