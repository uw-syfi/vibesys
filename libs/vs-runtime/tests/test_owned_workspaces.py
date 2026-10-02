"""Public composition contracts for runtime-owned workspace resources."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import TYPE_CHECKING

import pytest

from vs_agent.api import NULL_AGENT_EVENT_SINK
from vs_runtime.api import RuntimeContractError
from vs_runtime.api.infrastructure import (
    AgentExecutionScope,
    BlockingOperations,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    WorkspaceEvaluationSpec,
    WorkspaceRuntime,
    create_run_control_channel,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeRunControlEventSink,
    FakeWorkspace,
)
from vs_sandbox.api import SandboxExecutionResult

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import AgentRole
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration


class _Resource:
    def __init__(self, identifier: str | None, path: Path, revision: str = "a" * 40) -> None:
        self.id = identifier
        self.path = path
        self.revision: str | None = revision
        self.trusted_input_baseline: str | None = revision
        self.closed = False
        self.executions: list[tuple[str, int | None]] = []
        self.scripted: deque[SandboxExecutionResult] = deque()
        self.unavailable_revisions: set[str] = set()
        self.accuracy_calls: list[str | None] = []
        self.benchmark_calls: list[tuple[str | None, frozenset[str]]] = []

    def snapshot(self, label: str) -> str:
        self.revision = f"{len(label):040x}"
        return self.revision

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool:
        del clean, preserve_paths, preserve_memory
        self.revision = revision
        return True

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        return self.restore(revision, clean=clean)

    def retain(self, revision: str, reference: str) -> None:
        del revision, reference

    def pending_changes(self) -> list[str]:
        return []

    def candidate_patch(self, revision: str) -> str:
        if revision in self.unavailable_revisions:
            raise ValueError(revision)
        return revision

    def trusted_input_changes(self) -> list[str]:
        return []

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def execute(self, command: str, timeout_seconds: int | None) -> SandboxExecutionResult:
        self.executions.append((command, timeout_seconds))
        return self.scripted.popleft() if self.scripted else SandboxExecutionResult("", 0)

    def agent_scope(self) -> AgentExecutionScope:
        pytest.fail("workspace-only test requested an agent execution scope")

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        return WorkspaceEvaluationSpec(
            accuracy_command="python accuracy.py",
            benchmark_command="python benchmark.py",
            benchmark_contract=None,
            deployment_release_env_var=None,
        )

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult:
        self.accuracy_calls.append(command_override)
        return TrustedAccuracyResult(
            command=command_override,
            executed=True,
            passed=True,
        )

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult:
        self.benchmark_calls.append((command_override, required_metrics))
        return TrustedBenchmarkResult(
            command=command_override,
            executed=True,
            passed=True,
        )

    def close(self) -> None:
        self.closed = True


class _Provider:
    supports_parallel_candidates = True

    def __init__(self, path: Path) -> None:
        self.root = _Resource(None, path)
        self.created: list[_Resource] = []

    def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
        resource = _Resource(workspace_id, self.root.path / workspace_id, revision)
        self.created.append(resource)
        return resource


def _runtime(provider: _Provider) -> WorkspaceRuntime:
    def unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
        pytest.fail("workspace-only test opened an agent execution")

    def unexpected_client(**_kwargs: object) -> AgentClientProtocol:
        pytest.fail("workspace-only test opened an agent client")

    return create_workspace_runtime(
        (),
        workspace_resources=provider,
        resolve_configuration=unexpected_execution,
        session_store=lambda: None,
        control=create_run_control_channel(FakeRunControlEventSink()),
        lifecycle_events=FakeAgentExecutionLifecycleSink(),
        agent_events=NULL_AGENT_EVENT_SINK,
        route_message=lambda message, _steering: message,
        blocking=BlockingOperations(),
        client_factory=unexpected_client,
    )


def test_collection_owns_candidate_cleanup_in_reverse_order(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)

    async def exercise() -> None:
        runtime = _runtime(provider)
        workspaces = runtime.workspaces
        first = await workspaces.create_candidate()
        second = await workspaces.create_candidate()
        first_id = first.id
        second_id = second.id
        await workspaces.close()
        assert second_id is not None
        assert first_id is not None
        assert all(resource.closed for resource in provider.created)
        assert provider.root.closed
        with pytest.raises(ValueError, match="closed"):
            _ = first.path
        with pytest.raises(ValueError, match="closed"):
            _ = workspaces.root.path

    asyncio.run(exercise())


def test_close_attempts_every_candidate_and_aggregates_failures(tmp_path: Path) -> None:
    class _FailingResource(_Resource):
        def close(self) -> None:
            self.closed = True
            raise OSError(self.id)

    class _FailingProvider(_Provider):
        def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
            resource = _FailingResource(workspace_id, self.root.path / workspace_id, revision)
            self.created.append(resource)
            return resource

    provider = _FailingProvider(tmp_path)

    async def exercise() -> None:
        runtime = _runtime(provider)
        workspaces = runtime.workspaces
        await workspaces.create_candidate()
        await workspaces.create_candidate()
        with pytest.raises(BaseExceptionGroup) as captured:
            await workspaces.close()
        assert len(captured.value.exceptions) == 2
        assert all(resource.closed for resource in provider.created)

    asyncio.run(exercise())


def test_workspace_mutations_are_serialized(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingResource(_Resource):
        def __init__(self, identifier: str | None, path: Path) -> None:
            super().__init__(identifier, path)
            self.calls = 0
            self.active = 0
            self.maximum_active = 0

        def snapshot(self, label: str) -> str:
            self.calls += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.calls == 1:
                entered.set()
                release.wait()
            try:
                return super().snapshot(label)
            finally:
                self.active -= 1

    provider = _Provider(tmp_path)
    root = _BlockingResource(None, tmp_path)
    provider.root = root

    async def exercise() -> None:
        workspaces = _runtime(provider).workspaces
        first = asyncio.create_task(workspaces.root.snapshot("first"))
        await asyncio.to_thread(entered.wait)
        second_scheduled = asyncio.Event()

        async def second_snapshot() -> str:
            second_scheduled.set()
            return await workspaces.root.snapshot("second")

        second = asyncio.create_task(second_snapshot())
        await second_scheduled.wait()
        assert root.calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert root.maximum_active == 1
        await workspaces.close()

    asyncio.run(exercise())


def test_collection_rejects_foreign_handles_and_early_discard_is_idempotent(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)

    async def exercise() -> None:
        runtime = _runtime(provider)
        workspaces = runtime.workspaces
        candidate = await workspaces.create_candidate()
        candidate_id = candidate.id
        with pytest.raises(TypeError, match="live handle"):
            await runtime.commands.run(("true",), workspace=FakeWorkspace(path=tmp_path))
        await candidate.discard()
        await candidate.discard()
        assert candidate_id is not None
        with pytest.raises(ValueError, match="closed"):
            await runtime.commands.run(("true",), workspace=candidate)
        await workspaces.close()

    asyncio.run(exercise())


def test_runtime_commands_capture_and_remove_managed_output(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)
    provider.root.scripted.extend(
        (
            SandboxExecutionResult("runtime-result\n", 0, stdout="runtime-result\n"),
            SandboxExecutionResult("", 0),
            SandboxExecutionResult("captured", 0, stdout="captured"),
            SandboxExecutionResult("", 0),
        )
    )

    async def exercise() -> None:
        runtime = _runtime(provider)
        result = await runtime.commands.capture_output(
            ("profiler",),
            workspace=runtime.workspaces.root,
            output_argument="--output",
            timeout_seconds=17,
        )
        assert result.output == "captured"
        assert provider.root.executions == [
            ("mktemp", None),
            ("profiler --output runtime-result", 17),
            ("cat runtime-result", None),
            ("rm -f runtime-result", None),
        ]
        await runtime.workspaces.close()

    asyncio.run(exercise())


def test_runtime_evaluation_validates_and_normalizes_unavailable_revisions(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)
    provider.root.unavailable_revisions.add("missing")

    async def exercise() -> None:
        runtime = _runtime(provider)
        assert await runtime.evaluation.revisions_equivalent(
            runtime.workspaces.root,
            "same",
            "same",
        )
        with pytest.raises(RuntimeContractError, match="revision is unavailable"):
            await runtime.evaluation.revisions_equivalent(
                runtime.workspaces.root,
                "missing",
                "same",
            )
        with pytest.raises(TypeError, match="live handle"):
            runtime.evaluation.spec(FakeWorkspace(path=tmp_path))
        await runtime.workspaces.close()

    asyncio.run(exercise())


def test_runtime_evaluation_owns_snapshots_receipts_and_command_binding(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)

    async def exercise() -> None:
        runtime = _runtime(provider)
        workspace = runtime.workspaces.root
        accuracy = await runtime.evaluation.accuracy(
            "run-a",
            workspace,
            release=True,
        )
        benchmark = await runtime.evaluation.benchmark(
            workspace,
            required_metrics=frozenset({"throughput"}),
        )

        assert accuracy.result is not None
        assert accuracy.result.passed
        assert accuracy.receipt.run_id == "run-a"
        assert accuracy.receipt.workspace_id is None
        accuracy_command = provider.root.accuracy_calls[0]
        assert accuracy_command is not None
        assert accuracy.receipt.revision in accuracy_command
        assert "python accuracy.py" in accuracy_command
        assert benchmark.result.passed
        assert provider.root.benchmark_calls[0][1] == frozenset({"throughput"})
        benchmark_command = provider.root.benchmark_calls[0][0]
        assert benchmark_command is not None
        assert "python benchmark.py" in benchmark_command

        with pytest.raises(RuntimeContractError, match="another run"):
            await runtime.evaluation.accuracy(
                "run-b",
                workspace,
                reuse=accuracy.receipt,
            )
        # An agent-reported artifact path that is not a workspace file is
        # repairable feedback, including recipe JSON inlined in its place.
        for artifact in (
            "../recipes.json",
            '{"version":1,"recipes":[{"name":"a","command":"python3 -c \\"import re\\nok=True\\""}]}',
        ):
            with pytest.raises(
                LocalValidationRecipeError, match="workspace-relative file path"
            ) as raised:
                await runtime.evaluation.validate_local(
                    workspace,
                    recipe_artifact=artifact,
                    report_location="validation/report.json",
                )
            assert raised.value.kind is LocalValidationRecipeErrorKind.INVALID_ARTIFACT
        # The report location is framework-owned, so an invalid one stays a contract error.
        with pytest.raises(ValueError, match="writable path") as raised_report:
            await runtime.evaluation.validate_local(
                workspace,
                recipe_artifact="validation/recipes.json",
                report_location="../report.json",
            )
        assert not isinstance(raised_report.value, LocalValidationRecipeError)
        await runtime.workspaces.close()

    asyncio.run(exercise())


def test_cancelled_candidate_construction_drains_and_closes_partial_resource(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingProvider(_Provider):
        def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
            started.set()
            release.wait()
            return super().create_candidate(workspace_id, revision)

    provider = _BlockingProvider(tmp_path)

    async def exercise() -> None:
        workspaces = _runtime(provider).workspaces
        construction = asyncio.create_task(workspaces.create_candidate())
        await asyncio.to_thread(started.wait)
        construction.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await construction
        assert len(provider.created) == 1
        assert provider.created[0].closed
        await workspaces.close()

    asyncio.run(exercise())


def test_cancelled_close_keeps_owned_root_cleanup_alive(tmp_path: Path) -> None:
    close_started = threading.Event()
    close_release = threading.Event()

    class _BlockingRoot(_Resource):
        def close(self) -> None:
            close_started.set()
            close_release.wait()
            super().close()

    provider = _Provider(tmp_path)
    root = _BlockingRoot(None, tmp_path)
    provider.root = root

    async def exercise() -> None:
        workspaces = _runtime(provider).workspaces
        waiter = asyncio.create_task(workspaces.close())
        await asyncio.to_thread(close_started.wait)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        close_release.set()
        await workspaces.close()
        assert root.closed

    asyncio.run(exercise())
