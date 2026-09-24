"""Top-level built-in agent orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.loops.agent.entrypoint_common import AgentBuiltinDefaults, agent_execution_for
from vibesys.loops.legacy_bridge import legacy_request

if TYPE_CHECKING:
    from vibesys.api.run_request import RunRequestLike
    from vibesys.runtime import VibeSysRuntime


class AgentOrchestration(AgentBuiltinDefaults):
    """Run the selected single- or multi-agent policy with normal outer cadence."""

    include_agent_state = True

    def execute(self, request: RunRequestLike, runtime: VibeSysRuntime) -> bool:
        """Select and execute the requested inner agent policy."""
        return agent_execution_for(legacy_request(request), outer_loop="agent").execute(
            request, runtime
        )
