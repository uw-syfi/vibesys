"""Small plugin declarations for product-host capability tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from vs_runtime.api import OrchestrationPlugin, RunStatus

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole, RunHost


class EmptyOptions(BaseModel):
    """No configuration for a capability-only test plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _UnexpectedOrchestrationError(AssertionError):
    def __init__(self) -> None:
        super().__init__("capability test plugins are not orchestrated")


async def _not_orchestrated(_host: RunHost, _options: BaseModel) -> RunStatus:
    raise _UnexpectedOrchestrationError


def capability_plugin(
    plugin_id: str,
    *,
    agents: tuple[AgentRole, ...] = (),
    state: type[BaseModel] | None = None,
    memory_paths: tuple[str, ...] = (),
) -> OrchestrationPlugin:
    """Declare exactly the capabilities a direct product-host test needs."""
    return OrchestrationPlugin(
        id=plugin_id,
        agents=agents,
        options=EmptyOptions,
        orchestrate=_not_orchestrated,
        state=state,
        memory_paths=memory_paths,
    )


__all__ = ["EmptyOptions", "capability_plugin"]
