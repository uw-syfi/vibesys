"""Production adapter for the plugin-facing sandboxed command capability."""

# lint-waiver: LW-040118 [SLF001]; sibling host capabilities share private run-owned resources while the migration adapter exists.
# ruff: noqa: SLF001

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from vibesys.orchestration.workspaces import WorkspaceHandle
from vs_runtime.api import CommandResult, Workspace, validate_command

if TYPE_CHECKING:
    from vibesys.orchestration._host import HostResources


class _Commands:
    """Execute immutable argv requests through a run-owned workspace sandbox."""

    def __init__(self, host: HostResources) -> None:
        self._host = host

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run one safely quoted argv in the selected workspace environment."""
        validate_command(argv, timeout_seconds)
        if not isinstance(workspace, WorkspaceHandle):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        scope = self._host.workspaces._scope_of(workspace)
        context = self._host.workspaces._resources_for(scope)
        result = await self._host._run_blocking(
            context.run_environment_session.sandbox.execute,
            shlex.join(argv),
            timeout=timeout_seconds,
        )
        return CommandResult(
            output=result.output,
            exit_code=result.exit_code,
            truncated=result.truncated,
        )
