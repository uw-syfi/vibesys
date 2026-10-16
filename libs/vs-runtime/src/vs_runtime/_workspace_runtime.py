"""Runtime-owned commands and trusted evaluation over workspace handles."""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_project.api import GitError
from vs_runtime._build_outputs import (
    OUTPUTS_ENVIRONMENT_VARIABLE,
    MemoryBuildStore,
    MissingOutputError,
    build_key,
    verify_outputs,
)
from vs_runtime._local_validation import (
    LocalValidationEvents,
    LocalValidator,
    check_recipe_artifact_path,
)
from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces, WorkspaceResource, run_sync
from vs_runtime.contracts import (
    AccuracyReceipt,
    CaptureStopUnconfirmedError,
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

    from vs_runtime._build_outputs import BuildRecord, BuildStore
    from vs_runtime._local_validation import FrameworkValidationResult
    from vs_runtime._run_host import BlockingOperations
    from vs_runtime._trusted_evaluation import (
        TrustedAccuracyResult,
        TrustedBenchmarkContract,
        TrustedBenchmarkResult,
        TrustedBuild,
        TrustedBuildResult,
    )
    from vs_runtime._validation_passes import ValidationPasses
    from vs_sim.api import OutputSink


class CommandExecutionResult(Protocol):
    """The sandbox result fields needed by runtime command execution."""

    output: str
    exit_code: int | None
    truncated: bool
    stdout: str
    diagnostic_output: str
    diagnostic_stdout: str


@dataclass(frozen=True, slots=True)
class WorkspaceEvaluationSpec:
    """Immutable execution facts needed by product evaluation policy."""

    build: TrustedBuild | None
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
class RuntimeBuildRun:
    """Policy-neutral result of one build lifecycle: built, reused, or failed."""

    result: TrustedBuildResult | None
    """The build command's result; None when a recorded build was reused or none is declared."""
    reused: bool
    missing_output: str | None = None
    """The first required output the build did not produce, which fails the build."""

    @property
    def passed(self) -> bool:
        """Whether the commit has a verified build its later stages can load."""
        return self.missing_output is None and (self.result is None or self.result.passed)


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkRun:
    """Policy-neutral result and contract from one benchmark lifecycle."""

    result: TrustedBenchmarkResult
    contract: TrustedBenchmarkContract | None


@dataclass(frozen=True, slots=True)
class WorkspaceRuntime:
    """Composition-only bundle of capabilities sharing workspace ownership."""

    workspaces: RuntimeWorkspaces
    commands: RuntimeCommands
    capture_commands: RuntimeCaptureCommands
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


class RuntimeCaptureCommands:
    """Capture output through each environment's explicit capture-command runner."""

    def __init__(self, workspaces: RuntimeWorkspaces, blocking: BlockingOperations) -> None:
        self._workspaces = workspaces
        self._blocking = blocking

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run argv with a shared-workspace temporary artifact owned by this runtime."""
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        managed = self._workspaces.workspace_for(workspace)
        async with self._workspaces._mutation(managed):  # noqa: SLF001  # lint-waiver: LW-228423 [SLF001]; the sibling runtime capability serializes capture and cleanup through the workspace owner.
            resource = self._workspaces.resource_for(managed)
            # A relative path is visible at the same workspace root in an editor
            # container and in a Slurm job. mktemp creates it mode 0600 before
            # the command starts, so the command never chooses the artifact.
            temporary = await self._blocking.run(
                resource.execute, "mktemp .vibesys-capture.XXXXXXXXXX", None
            )
            if temporary.exit_code != 0 or not temporary.stdout.strip():
                message = "runtime could not allocate a captured command output file"
                raise RuntimeContractError(message)
            output_path = temporary.stdout.strip()
            await self._workspaces.begin_capture(managed, resource.capture_execution)
            primary_error: BaseException | None = None
            stop_unconfirmed = False
            try:
                command = shlex.join((*argv, output_argument, output_path))
                result = await self._blocking.run_cancellable(
                    lambda cancel: resource.execute_capture(command, timeout_seconds, cancel)
                )
                if result.exit_code != 0:
                    return _command_result(result)
                captured = await self._blocking.run(
                    resource.execute,
                    shlex.join(("cat", output_path)),
                    None,
                )
                return _captured_result(result, captured)
            except CaptureStopUnconfirmedError as error:
                primary_error = error
                stop_unconfirmed = True
                raise
            except BaseException as error:
                primary_error = error
                raise
            finally:
                if not stop_unconfirmed:
                    try:
                        await self._workspaces.finish_capture(managed)
                    except CaptureStopUnconfirmedError:
                        stop_unconfirmed = True
                        raise
                    cleanup_error = await self._remove_captured_output(resource, output_path)
                    if cleanup_error is not None:
                        if primary_error is None:
                            raise cleanup_error
                        primary_error.add_note(str(cleanup_error))

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

    def __init__(
        self,
        workspaces: RuntimeWorkspaces,
        commands: RuntimeCommands,
        validation_passes: ValidationPasses,
        builds: BuildStore | None = None,
    ) -> None:
        """``builds`` keeps verified candidate builds outside every workspace (default: memory)."""
        self._workspaces = workspaces
        self._commands = commands
        self._validator = LocalValidator(commands, validation_passes)
        self._builds = builds or MemoryBuildStore()

    def spec(self, workspace: Workspace) -> WorkspaceEvaluationSpec:
        return self._workspaces.resource_for(
            self._workspaces.workspace_for(workspace)
        ).evaluation_spec

    async def build(self, workspace: Workspace, *, output: OutputSink) -> RuntimeBuildRun:
        """Build the workspace's commit confined in a fresh checkout, or reuse its verified build.

        The build command's output is written to *output* as it runs. After it passes, every
        output the evaluator requires is verified and recorded outside the workspace, so the
        check and measure stages of this commit load exactly what this build produced.
        """
        managed = self._workspaces.workspace_for(workspace)
        build = self.spec(managed).build
        if build is None:
            return RuntimeBuildRun(result=None, reused=False)
        revision = await managed.snapshot("framework-build-input")
        key = build_key(revision, build.command, build.outputs)
        if self._builds.find(key) is not None:
            return RuntimeBuildRun(result=None, reused=True)
        async with self._evaluation_target(managed, revision) as target:
            result = await self._workspaces._evaluate(  # noqa: SLF001  # lint-waiver: LW-959101 [SLF001]; the candidate build must not overlap workspace mutation, like every trusted stage.
                # > A public mutation-scoped evaluate on RuntimeWorkspaces would be a second
                # > entry used only by its sibling capability; the gates use this one.
                target,
                lambda resource: resource.candidate_build(output),
            )
            if not result.passed:
                return RuntimeBuildRun(result=result, reused=False)
            try:
                verified = verify_outputs(target.path, build.outputs)
            except MissingOutputError as missing:
                return RuntimeBuildRun(result=result, reused=False, missing_output=str(missing))
        self._builds.record(key, revision, verified)
        return RuntimeBuildRun(result=result, reused=False)

    def _place_build_outputs(self, target: RuntimeWorkspace, revision: str) -> str | None:
        """Put the commit's verified build outputs in ``target``; their env assignment, or None.

        A task without a build has nothing to place. A task with one whose commit has no
        recorded build stops the stage: its build stage did not run or did not pass.
        """
        build = self.spec(target).build
        if build is None:
            return None
        record: BuildRecord | None = self._builds.find(
            build_key(revision, build.command, build.outputs)
        )
        if record is None:
            message = (
                f"no verified build of revision {revision} is recorded: the measurement's "
                "build stage did not run or did not pass"
            )
            raise RuntimeContractError(message)
        self._builds.materialize(record, target.path)
        paths = ":".join(output.path for output in record.outputs)
        return f"{OUTPUTS_ENVIRONMENT_VARIABLE}={shlex.quote(paths)}"

    async def accuracy(
        self,
        run_id: str,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        release: bool = False,
        output: OutputSink,
    ) -> RuntimeAccuracyRun:
        """Run accuracy or validate explicit reuse for the current candidate.

        The accuracy command's output is written to *output* as it runs; a reuse runs nothing.
        """
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
                outputs=self._place_build_outputs(target, candidate_revision),
            )
            result = await self._workspaces._evaluate(  # noqa: SLF001  # lint-waiver: LW-228425 [SLF001]; trusted execution must not overlap workspace mutation.
                target, lambda resource: resource.trusted_accuracy(command_override, output)
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
        output: OutputSink,
    ) -> RuntimeBenchmarkRun:
        """Snapshot and benchmark the exact submitted candidate revision.

        The benchmark command's output is written to *output* as it runs.
        """
        managed = self._workspaces.workspace_for(workspace)
        candidate_revision = await managed.snapshot("framework-benchmark-input")
        async with self._evaluation_target(managed, candidate_revision) as target:
            spec = self.spec(target)
            command_override = _evaluation_command(
                spec.benchmark_command,
                candidate_revision,
                spec.deployment_release_env_var,
                outputs=self._place_build_outputs(target, candidate_revision),
            )
            result = await self._workspaces._evaluate(  # noqa: SLF001  # lint-waiver: LW-228426 [SLF001]; trusted execution must not overlap workspace mutation.
                target,
                lambda resource: resource.trusted_benchmark(
                    command_override, required_metrics, output
                ),
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
        return await self._validator.run(
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
            except (OSError, RuntimeError, ValueError, GitError) as error:
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

    async def _blocking_patch(self, resource: WorkspaceResource, revision: str) -> str:
        return await run_sync(self._workspaces.runner, resource.candidate_patch, revision)


def _evaluation_command(
    command: str | None,
    revision: str | None,
    release_env: str | None,
    *,
    outputs: str | None = None,
) -> str | None:
    if command is None:
        return None
    variables = []
    if revision:
        variables.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(revision)}")
    if outputs:
        variables.append(outputs)
    if release_env:
        variables.append(f"{release_env}=1")
    return f"env {' '.join(variables)} {command}" if variables else command


def _command_result(result: CommandExecutionResult) -> CommandResult:
    return CommandResult(
        output=result.output,
        exit_code=result.exit_code,
        truncated=result.truncated,
        diagnostic_output=result.diagnostic_output,
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
        diagnostic_output=captured.diagnostic_stdout,
    )
