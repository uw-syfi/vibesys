"""Production adapter for the plugin-facing sandboxed command capability."""

# lint-waiver: LW-040118 [SLF001]; sibling host capabilities share private run-owned resources while the migration adapter exists.
# ruff: noqa: SLF001

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from vibesys.orchestration.workspaces import WorkspaceHandle
from vs_runtime.api import CommandResult, RuntimeContractError, Workspace, validate_command

if TYPE_CHECKING:
    from vibesys.orchestration._host import HostResources
    from vs_sandbox.api import Sandbox, SandboxExecutionResult


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

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Capture a command's runtime-managed output artifact and remove it."""
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        if not isinstance(workspace, WorkspaceHandle):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        scope = self._host.workspaces._scope_of(workspace)
        context = self._host.workspaces._resources_for(scope)
        sandbox = context.run_environment_session.sandbox
        temporary = await self._host._run_blocking(
            sandbox.execute,
            "mktemp",
        )
        if temporary.exit_code != 0 or not temporary.stdout.strip():
            message = "runtime could not allocate a captured command output file"
            raise RuntimeContractError(message)
        output_path = temporary.stdout.strip()
        primary_error: BaseException | None = None
        try:
            result = await self._host._run_blocking(
                sandbox.execute,
                shlex.join((*argv, output_argument, output_path)),
                timeout=timeout_seconds,
            )
            if result.exit_code != 0:
                return CommandResult(
                    output=result.output,
                    exit_code=result.exit_code,
                    truncated=result.truncated,
                )
            captured = await self._host._run_blocking(
                sandbox.execute,
                shlex.join(("cat", output_path)),
            )
            return _captured_result(result, captured)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_error = await self._remove_captured_output(sandbox, output_path)
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                primary_error.add_note(str(cleanup_error))

    async def _remove_captured_output(
        self,
        sandbox: Sandbox,
        output_path: str,
    ) -> RuntimeContractError | None:
        try:
            cleanup = await self._host._run_blocking(
                sandbox.execute,
                shlex.join(("rm", "-f", output_path)),
            )
        except (OSError, RuntimeError, ValueError) as cause:
            error = RuntimeContractError("runtime could not remove a captured command output file")
            error.add_note(f"cleanup raised {type(cause).__name__}: {cause}")
            return error
        if cleanup.exit_code != 0:
            return RuntimeContractError("runtime could not remove a captured command output file")
        return None


def _captured_result(
    command: SandboxExecutionResult,
    captured: SandboxExecutionResult,
) -> CommandResult:
    if captured.exit_code != 0:
        message = "command completed without a readable captured output file"
        raise RuntimeContractError(message)
    return CommandResult(
        output=captured.stdout,
        exit_code=command.exit_code,
        truncated=captured.truncated,
    )
