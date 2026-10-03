"""Runtime-owned commands and trusted evaluation over workspace handles."""

from __future__ import annotations

import shlex
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_runtime._local_validation import (
    LocalValidationEvents,
    check_recipe_artifact_path,
    run_local_validation,
)
from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces, WorkspaceResource, run_sync
from vs_runtime.contracts import (
    AccuracyReceipt,
    CommandResult,
    RuntimeContractError,
    Workspace,
    WorkspaceAccess,
    validate_command,
    validate_trusted_shell_command,
    validate_workspace_writable_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_runtime._agent_sessions import RuntimeAgentSessions
    from vs_runtime._local_validation import FrameworkValidationResult
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
class RuntimeAccuracyRun:
    """Policy-neutral result of one accuracy lifecycle."""

    result: TrustedAccuracyResult | None
    receipt: AccuracyReceipt
    reused: bool
    command: str | None


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkRun:
    """Policy-neutral result and contract from one benchmark lifecycle."""

    result: TrustedBenchmarkResult
    contract: TrustedBenchmarkContract | None


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
    """Own the complete trusted-evaluation lifecycle for live workspaces."""

    def __init__(self, workspaces: RuntimeWorkspaces, commands: RuntimeCommands) -> None:
        self._workspaces = workspaces
        self._commands = commands

    def spec(self, workspace: Workspace) -> WorkspaceEvaluationSpec:
        return self._workspaces.resource_for(
            self._workspaces.workspace_for(workspace)
        ).evaluation_spec

    async def accuracy(
        self,
        run_id: str,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        release: bool = False,
    ) -> RuntimeAccuracyRun:
        """Run accuracy or validate explicit reuse for the current candidate."""
        managed = self._workspaces.workspace_for(workspace)
        spec = self.spec(managed)
        if reuse is not None:
            self._validate_receipt_owner(run_id, managed, reuse)
            await managed.snapshot("framework-accuracy-reuse-input")
            await self._validate_receipt_revision(managed, reuse)
            return RuntimeAccuracyRun(
                result=None,
                receipt=reuse,
                reused=True,
                command=spec.accuracy_command,
            )

        candidate_revision = await managed.snapshot("framework-accuracy-input")
        async with self._evaluation_target(managed, candidate_revision) as target:
            spec = self.spec(target)
            command_override = _evaluation_command(
                spec.accuracy_command,
                candidate_revision,
                spec.deployment_release_env_var if release else None,
            )
            result = await self._workspaces._evaluate(  # noqa: SLF001  # lint-waiver: LW-228425 [SLF001]; trusted execution must not overlap workspace mutation.
                target, lambda resource: resource.trusted_accuracy(command_override)
            )
        if result.executed and target is managed:
            await managed.snapshot("framework-accuracy-evaluation")
        return RuntimeAccuracyRun(
            result=result,
            receipt=AccuracyReceipt(
                run_id=run_id,
                workspace_id=managed.id,
                revision=candidate_revision,
            ),
            reused=False,
            command=result.command,
        )

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        required_metrics: frozenset[str],
    ) -> RuntimeBenchmarkRun:
        """Snapshot and benchmark the exact submitted candidate revision."""
        managed = self._workspaces.workspace_for(workspace)
        candidate_revision = await managed.snapshot("framework-benchmark-input")
        async with self._evaluation_target(managed, candidate_revision) as target:
            spec = self.spec(target)
            command_override = _evaluation_command(
                spec.benchmark_command,
                candidate_revision,
                spec.deployment_release_env_var,
            )
            result = await self._workspaces._evaluate(  # noqa: SLF001  # lint-waiver: LW-228426 [SLF001]; trusted execution must not overlap workspace mutation.
                target,
                lambda resource: resource.trusted_benchmark(command_override, required_metrics),
            )
        if result.executed and target is managed:
            await managed.snapshot("framework-benchmark-evaluation")
        return RuntimeBenchmarkRun(result=result, contract=spec.benchmark_contract)

    @asynccontextmanager
    async def _evaluation_target(
        self, managed: RuntimeWorkspace, revision: str
    ) -> AsyncIterator[RuntimeWorkspace]:
        """Yield the workspace a trusted command may run in for one submitted revision.

        An agent keeps editing its live workspace while a gate runs, and a remote
        sandbox stages the directory it executes in. Staging the live tree would
        race those edits and could evaluate content newer than the submitted
        revision. Where the environment can open isolated candidates, the gate
        runs in a throwaway candidate checked out at exactly ``revision``.
        Otherwise it runs in the live workspace.
        """
        if not self._workspaces.supports_parallel_candidates:
            yield managed
            return
        candidate = await self._workspaces.create_candidate(revision)
        try:
            yield self._workspaces.workspace_for(candidate)
        finally:
            await candidate.discard()

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
        events: LocalValidationEvents | None = None,
    ) -> tuple[FrameworkValidationResult, ...]:
        """Run candidate-authored recipes through runtime-owned commands."""
        check_recipe_artifact_path(recipe_artifact)
        validate_workspace_writable_paths(WorkspaceAccess.LIMITED, (report_location,))
        managed = self._workspaces.workspace_for(workspace)
        return await run_local_validation(
            self._commands,
            managed,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
            events=events,
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
    def _validate_receipt_owner(
        run_id: str,
        workspace: Workspace,
        receipt: AccuracyReceipt,
    ) -> None:
        if receipt.run_id != run_id:
            message = "accuracy receipt belongs to another run"
            raise RuntimeContractError(message)
        if receipt.workspace_id != workspace.id:
            message = "accuracy receipt belongs to another workspace"
            raise RuntimeContractError(message)

    async def _validate_receipt_revision(
        self,
        workspace: Workspace,
        receipt: AccuracyReceipt,
    ) -> None:
        current_revision = workspace.revision
        if current_revision is None:
            message = "accuracy receipt does not match the current workspace revision"
            raise RuntimeContractError(message)
        if receipt.revision == current_revision:
            return
        if not await self.revisions_equivalent(workspace, receipt.revision, current_revision):
            message = "accuracy receipt does not match the current candidate revision"
            raise RuntimeContractError(message)

    @staticmethod
    async def _blocking_patch(resource: WorkspaceResource, revision: str) -> str:
        return await run_sync(resource.candidate_patch, revision)


def _evaluation_command(
    command: str | None,
    revision: str | None,
    release_env: str | None,
) -> str | None:
    if command is None:
        return None
    variables = []
    if revision:
        variables.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(revision)}")
    if release_env:
        variables.append(f"{release_env}=1")
    return f"env {' '.join(variables)} {command}" if variables else command


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
