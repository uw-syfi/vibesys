"""Authoritative agent run state models and their durable storage.

# TODO(stack PR 09): remove. Glue re-export: the state types now live in
# ``vibesys.search.hypothesis.state`` and ``vibesys.search.profile_focus.state``
# (pure, no I/O). This module keeps the old import path and the mutating
# ``AgentRunStateStore.save``/``.transition`` methods (not owned by ``search``,
# which does no I/O) working for ``vibesys.api.chat_tools_server`` and its
# test, whose migration in stack PR 09 removes the last caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.search.hypothesis.state import (
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisStrategy,
)
from vibesys.search.hypothesis.state import HypothesisState as AgentRunState
from vibesys.search.profile_focus.state import (
    ProfileAttributionSample,
    ProfileBottleneck,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
    ProfileImprovementSample,
)
from vibesys.search.profile_focus.state import ProfileFocusState as ProfileGuidanceState

if TYPE_CHECKING:
    from vs_project.api import Project, StateNamespace

__all__ = [
    "AgentRunState",
    "AgentRunStateStore",
    "Hypothesis",
    "HypothesisMeasurement",
    "HypothesisResolution",
    "HypothesisReview",
    "HypothesisStrategy",
    "ProfileAttributionSample",
    "ProfileBottleneck",
    "ProfileGuidanceState",
    "ProfileGuidanceStatus",
    "ProfileGuidedComponent",
    "ProfileImprovementSample",
    "load_agent_run_state",
]


class AgentRunStateStore:
    """Persist agent policy state in the run's portable v4 namespace."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the single typed state slot in the supplied namespace."""
        self._namespace = namespace
        self._slot = namespace.slot("state.json", AgentRunState)

    def load_optional(self) -> AgentRunState | None:
        """Return the authoritative aggregate when present."""
        return self._slot.load_optional()

    def load(self) -> AgentRunState:
        """Return the aggregate or a new empty state."""
        return self.load_optional() or AgentRunState()

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace used for Git snapshots."""
        return self._namespace


def load_agent_run_state(project: Project, run_id: str, *, namespace: str) -> AgentRunState | None:
    """Load one agent run's state without legacy format recovery."""
    return AgentRunStateStore(project.state.portable_namespace(run_id, namespace)).load_optional()
