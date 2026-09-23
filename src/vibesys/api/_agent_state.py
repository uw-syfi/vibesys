"""Load one run's `AgentRunState` from a `vs_project.Project`, or `None`.

Private to `vibesys.api`. Matches the agent-state read path
`server.api.service.RunApi` now sources from this module (via
`vibesys.api.open_run_store`) field-for-field, minus the server's own
in-memory caching (`vibesys.api` has no request-scoped cache to invalidate):
load the unified state file if present, else migrate the legacy
ledger/checkpoint files, then reproject hypothesis summaries from round
evidence either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from vibesys.loops.agent.hypotheses import reproject_run_evidence
from vibesys.loops.agent.orchestration import (
    AGENT_ORCHESTRATION_IDS,
    configuration_from_manifest,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.metrics import MetricSpace, Objective
from vibesys.run.state import RunStateNamespace
from vs_project.api import AgentRunConfiguration, OrchestrationRunManifest

if TYPE_CHECKING:
    from vibesys.loops.agent.model import AgentRunState
    from vs_project.api import Project, RunManifestRecord

# The server's own experiment/design/performance projections treat only
# "agent" as an agent run, deliberately excluding "profile-guided" even
# though both outer loops persist `AgentRunState` (see
# `server.api.service.RunApi._is_agent_run`). Matched here so a run's
# `RunView.hypotheses`/`rounds` agree with what the server would show for the
# same run today.
_AGENT_OUTER_LOOP = "agent"


def agent_run_objectives(manifest: RunManifestRecord) -> tuple[str, ...] | None:
    """Return validated objective axes for an agent run, or ``None``."""
    if isinstance(manifest, OrchestrationRunManifest):
        if manifest.orchestration.id not in AGENT_ORCHESTRATION_IDS:
            return None
        return configuration_from_manifest(manifest).objectives
    if isinstance(manifest.configuration, AgentRunConfiguration):
        return manifest.configuration.objectives
    return None


def is_agent_run_manifest(manifest: RunManifestRecord) -> bool:
    """Identify the agent run kind from metadata without loading round state."""
    if isinstance(manifest, OrchestrationRunManifest):
        return manifest.orchestration.id == _AGENT_OUTER_LOOP
    return (
        isinstance(manifest.configuration, AgentRunConfiguration)
        and manifest.configuration.outer_loop == _AGENT_OUTER_LOOP
    )


def load_agent_run_state(project: Project, run_id: str) -> AgentRunState | None:
    """Return *run_id*'s reprojected agent state, or `None` for a non-agent run."""
    manifest = project.state.load_run(run_id)
    if isinstance(manifest, OrchestrationRunManifest):
        if manifest.orchestration.id != _AGENT_OUTER_LOOP:
            return None
        configuration = configuration_from_manifest(manifest)
    else:
        configuration = manifest.configuration
    if not isinstance(configuration, AgentRunConfiguration):
        return None
    if configuration.outer_loop != _AGENT_OUTER_LOOP:
        return None
    portable = project.state.portable_namespace(run_id, "agent")
    store = AgentRunStateStore(portable)
    state = store.load_optional()
    if state is not None:
        return reproject_run_evidence(state)
    local = project.state.local_namespace(run_id, RunStateNamespace.AGENT)
    # Unified state predating the persisted metric space: the run manifest
    # records the axes but no tolerance, so legacy rounds compare exactly
    # (matches the server's own migration call before it moved onto this
    # module).
    return store.migrate_legacy(
        rounds=project.state.load_rounds(run_id),
        local_namespace=local,
        legacy_space=MetricSpace(
            objectives=tuple(
                Objective(name=name, direction=direction)
                for name, direction in _metric_directions(configuration.objectives).items()
            )
        ),
    )


def _metric_directions(encoded: tuple[str, ...]) -> dict[str, Literal["max", "min"]]:
    """Decode `"metric:direction"`-encoded objective directions.

    Matches `server.api.performance.metric_directions`, duplicated rather
    than imported: that module is server-private.
    """
    directions: dict[str, Literal["max", "min"]] = {}
    for value in encoded:
        name, separator, direction = value.rpartition(":")
        if separator and name:
            if direction == "max":
                directions[name] = "max"
            elif direction == "min":
                directions[name] = "min"
    return directions
