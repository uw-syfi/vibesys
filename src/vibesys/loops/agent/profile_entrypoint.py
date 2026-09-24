"""Top-level profile-guided agent orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.entrypoint_common import AgentBuiltinDefaults, agent_execution_for
from vibesys.loops.legacy_bridge import legacy_request

if TYPE_CHECKING:
    from vibesys.orchestration.request import RunRequestLike
    from vibesys.runtime import VibeSysRuntime


class ProfileGuidedOrchestration(AgentBuiltinDefaults):
    """Wrap the selected inner agent policy with profile-guided control flow."""

    include_agent_state = False

    def execute(self, request: RunRequestLike, runtime: VibeSysRuntime) -> bool:
        """Execute the selected inner policy under profile guidance."""
        return agent_execution_for(legacy_request(request), outer_loop="profile-guided").execute(
            request, runtime
        )
