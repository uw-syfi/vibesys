"""Runtime-owned commands and trusted evaluation over workspace handles."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_runtime._workspaces import RuntimeWorkspaces, WorkspaceResource, run_sync
from vs_runtime.contracts import (
    CommandResult,
    RuntimeContractError,
    Workspace,
    validate_command,
    validate_trusted_shell_command,
)

if TYPE_CHECKING:
    from vs_runtime._agent_sessions import RuntimeAgentSessions
    from vs_runtime._run_host import BlockingOperations
    from vs_runtime._trusted_evaluation import (
        TrustedAccuracyResult,
        TrustedBenchmarkContract,
        TrustedBenchmarkResult,
    )


class CommandExecutionResult(Protocol):
    """The sandbox result fields needed by runtime command execution."""

    output: str
    exit_code: int | None
    truncated: bool
    stdout: str


@dataclass(frozen=True, slots=True)
class WorkspaceEvaluationSpec:
    """Immutable execution facts needed by product evaluation policy."""

    accuracy_command: str | None
    benchmark_command: str | None
    benchmark_contract: TrustedBenchmarkContract | None
    deployment_release_env_var: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceRuntime:
    """Composition-only bundle of capabilities sharing workspace ownership."""

    agents: RuntimeAgentSessions
    workspaces: RuntimeWorkspaces
    commands: RuntimeCommands
    evaluation: RuntimeWorkspaceEvaluation


class RuntimeCommands:
    """Execute commands through validated, runtime-owned workspace resources."""

    def __init__(self, workspaces: RuntimeWorkspaces, blocking: BlockingOperations) -> None:
        self._workspaces = workspaces
        self._blocking = blocking

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        validate_command(argv, timeout_seconds)
        return await self._execute(workspace, shlex.join(argv), timeout_seconds)

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228423 [SLF001]; the sibling runtime capability serializes effects through the workspace owner.
            resource = self._workspaces.resource_for(managed)
            temporary = await self._blocking.run(resource.execute, "mktemp", None)
            if temporary.exit_code != 0 or not temporary.stdout.strip():
                message = "runtime could not allocate a captured command output file"
                raise RuntimeContractError(message)
            output_path = temporary.stdout.strip()
            primary_error: BaseException | None = None
            try:
                result = await self._blocking.run(
                    resource.execute,
                    shlex.join((*argv, output_argument, output_path)),
                    timeout_seconds,
                )
                if result.exit_code != 0:
                    return _command_result(result)
                captured = await self._blocking.run(
                    resource.execute,
                    shlex.join(("cat", output_path)),
                    None,
                )
                return _captured_result(result, captured)
            except BaseException as error:
                primary_error = error
                raise
            finally:
                cleanup_error = await self._remove_captured_output(resource, output_path)
                if cleanup_error is not None:
                    if primary_error is None:
                        raise cleanup_error
                    primary_error.add_note(str(cleanup_error))

    async def run_trusted_shell(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        validate_trusted_shell_command(command, timeout_seconds)
        return await self._execute(workspace, command, timeout_seconds)

    async def _execute(
        self,
        workspace: Workspace,
        command: str,
        timeout_seconds: int | None,
    ) -> CommandResult:
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228424 [SLF001]; the sibling runtime capability serializes effects through the workspace owner.
            resource = self._workspaces.resource_for(managed)
            result = await self._blocking.run(resource.execute, command, timeout_seconds)
        return _command_result(result)

    async def _remove_captured_output(
        self,
        resource: WorkspaceResource,
        output_path: str,
    ) -> RuntimeContractError | None:
        try:
            cleanup = await self._blocking.run(
                resource.execute,
                shlex.join(("rm", "-f", output_path)),
                None,
            )
        except (OSError, RuntimeError, ValueError) as cause:
            error = RuntimeContractError("runtime could not remove a captured command output file")
            error.add_note(f"cleanup raised {type(cause).__name__}: {cause}")
            return error
        if cleanup.exit_code != 0:
            return RuntimeContractError("runtime could not remove a captured command output file")
        return None


class RuntimeWorkspaceEvaluation:
    """Trusted evaluation operations over validated workspace handles."""

    def __init__(self, workspaces: RuntimeWorkspaces) -> None:
        self._workspaces = workspaces

    def spec(self, workspace: Workspace) -> WorkspaceEvaluationSpec:
        return self._workspaces.resource_for(
            self._workspaces.workspace_for(workspace)
        ).evaluation_spec

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        command_override: str | None,
    ) -> TrustedAccuracyResult:
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228425 [SLF001]; trusted execution must not overlap workspace mutation.
            return await self._workspaces.resource_for(managed).trusted_accuracy(command_override)

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult:
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228426 [SLF001]; trusted execution must not overlap workspace mutation.
            return await self._workspaces.resource_for(managed).trusted_benchmark(
                command_override,
                required_metrics,
            )

    async def revisions_equivalent(
        self,
        workspace: Workspace,
        left: str,
        right: str,
    ) -> bool:
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228427 [SLF001]; revision reads are serialized with workspace mutation.
            resource = self._workspaces.resource_for(managed)
            try:
                left_patch = await self._blocking_patch(resource, left)
                right_patch = await self._blocking_patch(resource, right)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                message = "accuracy receipt revision is unavailable in this workspace"
                raise RuntimeContractError(message) from error
        return left_patch == right_patch

    @staticmethod
    async def _blocking_patch(resource: WorkspaceResource, revision: str) -> str:
        return await run_sync(resource.candidate_patch, revision)


def _command_result(result: CommandExecutionResult) -> CommandResult:
    return CommandResult(
        output=result.output,
        exit_code=result.exit_code,
        truncated=result.truncated,
    )


def _captured_result(
    command: CommandExecutionResult,
    captured: CommandExecutionResult,
) -> CommandResult:
    if captured.exit_code != 0:
        message = "command completed without a readable captured output file"
        raise RuntimeContractError(message)
    return CommandResult(
        output=captured.stdout,
        exit_code=command.exit_code,
        truncated=captured.truncated,
    )
