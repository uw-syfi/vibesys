"""Public composition contracts for runtime-owned workspace resources."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import RuntimeContractError
from vs_runtime.api.core import BaselineEntry, RootBaselineDrift
from vs_runtime.api.infrastructure import (
    AgentExecutionScope,
    BlockingOperations,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    WorkspaceEvaluationSpec,
    WorkspaceRuntime,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeWorkspace,
    MemoryValidationPasses,
)
from vs_sandbox.api import CommandResult
from vs_sim.api.testing import GatedBlockingRunner, InlineBlockingRunner, arrival

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import AgentRole
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import OutputSink
from vs_sim.api import DISCARD


class _Resource:
    def __init__(self, identifier: str | None, path: Path, revision: str = "a" * 40) -> None:
        self.id = identifier
        self.path = path
        self.revision: str | None = revision
        self.trusted_input_baseline: str | None = revision
        self.closed = False
        self.detached = False
        self.executions: list[tuple[str, int | None]] = []
        self.scripted: deque[CommandResult] = deque()
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
        exact_keeping: tuple[str, ...] | None = None,
    ) -> bool:
        del clean, preserve_paths, preserve_memory, exact_keeping
        self.revision = revision
        return True

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        return self.restore(revision, clean=clean)

    def retain(self, revision: str, reference: str) -> None:
        del revision, reference

    def has_revision(self, revision: str) -> bool:
        del revision
        return True

    def unversioned_paths(self) -> tuple[str, ...]:
        return ()

    def baseline_entries(self, paths: tuple[str, ...]) -> tuple[BaselineEntry, ...]:
        return tuple(BaselineEntry(path=path) for path in paths)

    def baseline_drift(self, entries: tuple[BaselineEntry, ...]) -> RootBaselineDrift:
        del entries
        return RootBaselineDrift()

    def matches_revision(
        self, revision: str, *, exact_keeping: tuple[str, ...] | None = None
    ) -> bool:
        del revision, exact_keeping
        return True

    def find_snapshot(self, label: str) -> str | None:
        del label
        return None

    def pending_changes(self) -> list[str]:
        return []

    def candidate_patch(self, revision: str) -> str:
        if revision in self.unavailable_revisions:
            raise ValueError(revision)
        return revision

    def is_retained(self, revision: str) -> bool:
        return revision not in self.unavailable_revisions

    def trusted_input_changes(self) -> list[str]:
        return []

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def execute(self, command: str, timeout_seconds: int | None) -> CommandResult:
        self.executions.append((command, timeout_seconds))
        return self.scripted.popleft() if self.scripted else CommandResult("", 0)

    def agent_sandbox(self) -> CommandRunner | None:
        return None

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

    async def trusted_accuracy(
        self, command_override: str | None, output: OutputSink
    ) -> TrustedAccuracyResult:
        del output
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
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        del output
        self.benchmark_calls.append((command_override, required_metrics))
        return TrustedBenchmarkResult(
            command=command_override,
            executed=True,
            passed=True,
        )

    def close(self) -> None:
        self.closed = True

    def detach(self) -> None:
        self.detached = True


class _Provider:
    supports_parallel_candidates = True

    def __init__(self, path: Path) -> None:
        self.root = _Resource(None, path)
        self.created: list[_Resource] = []

    def reattach_candidate(self, workspace_id: str, revision: str) -> _Resource | None:
        del workspace_id, revision
        return None

    def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
        resource = _Resource(workspace_id, self.root.path / workspace_id, revision)
        self.created.append(resource)
        return resource

    def leftover_candidates(self) -> tuple[str, ...]:
        """No candidate outlives this provider: each lives only in ``created``."""
        return ()

    def remove_leftover(self, workspace_id: str, /) -> None:
        del workspace_id


class _LiveOnlyProvider(_Provider):
    """An environment that cannot open isolated candidates."""

    supports_parallel_candidates = False


class _ContentResource(_Resource):
    """A workspace whose files are versioned by snapshot and staged like a remote sandbox."""

    def __init__(
        self,
        identifier: str | None,
        path: Path,
        files: dict[str, str],
        history: dict[str, dict[str, str]],
    ) -> None:
        super().__init__(identifier, path)
        self.files = dict(files)
        self.history = history
        self.staged: list[dict[str, str]] = []
        self.edit_while_staging: Callable[[], None] = lambda: None

    def snapshot(self, label: str) -> str:
        del label
        self.revision = f"{len(self.history):040x}"
        self.history[self.revision] = dict(self.files)
        return self.revision

    async def trusted_accuracy(
        self, command_override: str | None, output: OutputSink
    ) -> TrustedAccuracyResult:
        before = dict(self.files)
        self.edit_while_staging()
        if dict(self.files) != before:
            message = "local input changed during remote staging"
            raise RuntimeError(message)
        self.staged.append(before)
        return await super().trusted_accuracy(command_override, output)


class _ContentProvider(_Provider):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.history: dict[str, dict[str, str]] = {}
        self.content_root = _ContentResource(None, path, {"engine.py": "v1"}, self.history)
        self.root = self.content_root
        self.candidates: list[_ContentResource] = []

    def reattach_candidate(self, workspace_id: str, revision: str) -> _ContentResource | None:
        del workspace_id, revision
        return None

    def create_candidate(self, workspace_id: str, revision: str) -> _ContentResource:
        resource = _ContentResource(
            workspace_id, self.root.path / workspace_id, self.history[revision], self.history
        )
        self.candidates.append(resource)
        self.created.append(resource)
        return resource


def _runtime(provider: _Provider, runner: GatedBlockingRunner | None = None) -> WorkspaceRuntime:
    def unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
        pytest.fail("workspace-only test opened an agent execution")

    def unexpected_client(**_kwargs: object) -> AgentClientProtocol:
        pytest.fail("workspace-only test opened an agent client")

    return create_workspace_runtime(
        workspace_resources=provider,
        blocking=BlockingOperations(runner or InlineBlockingRunner()),
        validation_passes=MemoryValidationPasses(),
    )


async def test_collection_owns_candidate_cleanup_in_reverse_order(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)
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


async def test_close_attempts_every_candidate_and_aggregates_failures(tmp_path: Path) -> None:
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
    runtime = _runtime(provider)
    workspaces = runtime.workspaces
    await workspaces.create_candidate()
    await workspaces.create_candidate()
    with pytest.raises(BaseExceptionGroup) as captured:
        await workspaces.close()
    assert len(captured.value.exceptions) == 2
    assert all(resource.closed for resource in provider.created)


async def test_close_keeps_member_candidates_and_discards_scratch_ones(tmp_path: Path) -> None:
    """A run close detaches what core holds (attempt members) and discards the rest."""
    provider = _Provider(tmp_path)
    runtime = _runtime(provider)
    workspaces = runtime.workspaces
    await workspaces.create_candidate(member_id="attempt-1")
    await workspaces.create_candidate()
    await workspaces.create_candidate(member_id="attempt-2")
    await workspaces.close()
    member, scratch, second = provider.created
    assert (member.detached, member.closed) == (True, False)
    assert (second.detached, second.closed) == (True, False)
    assert (scratch.detached, scratch.closed) == (False, True)
    assert provider.root.closed


class _CloseFailedError(OSError):
    pass


class _FlakyResource(_Resource):
    """A resource whose first ``failures`` closes fail, like a killed ``docker rm``."""

    def __init__(self, identifier: str | None, path: Path, failures: int) -> None:
        super().__init__(identifier, path)
        self.failures = failures
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.failures:
            raise _CloseFailedError
        self.closed = True


class _FlakyProvider(_Provider):
    def __init__(self, path: Path, failures: int, *, root_failures: int) -> None:
        super().__init__(path)
        self.failures = failures
        self.root = _FlakyResource(None, path, root_failures)

    def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
        del revision
        resource = _FlakyResource(workspace_id, self.root.path / workspace_id, self.failures)
        self.created.append(resource)
        return resource


@pytest.mark.parametrize("failures", [0, 1, 2])
@pytest.mark.parametrize("retried_by", ["discard", "close"])
async def test_a_failed_close_is_retried_until_the_resource_closes(
    tmp_path: Path, failures: int, retried_by: str
) -> None:
    """Regression: a candidate whose close failed was forgotten, so a retry reported success."""
    root_failures = failures if retried_by == "close" else 0
    provider = _FlakyProvider(tmp_path, failures, root_failures=root_failures)
    workspaces = _runtime(provider).workspaces
    candidate = await workspaces.create_candidate()
    (resource,) = provider.created
    assert isinstance(resource, _FlakyResource)
    root = provider.root
    assert isinstance(root, _FlakyResource)

    retry = candidate.discard if retried_by == "discard" else workspaces.close
    for _ in range(failures):
        with pytest.raises(BaseExceptionGroup):
            await retry()
    if retried_by == "discard":
        await candidate.discard()
    await workspaces.close()
    await candidate.discard()
    await workspaces.close()

    assert resource.closed
    assert resource.close_calls == failures + 1
    assert root.closed
    assert root.close_calls == root_failures + 1


async def test_workspace_mutations_are_serialized(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)
    runner = GatedBlockingRunner(held=True)
    workspaces = _runtime(provider, runner).workspaces
    first = asyncio.create_task(workspaces.root.snapshot("first"))
    await runner.wait_in_flight(1, first)
    second_scheduled = asyncio.Event()

    async def second_snapshot() -> str:
        second_scheduled.set()
        return await workspaces.root.snapshot("second")

    second = asyncio.create_task(second_snapshot())
    await arrival(second_scheduled.wait(), second)
    runner.release()
    await asyncio.gather(first, second)
    assert runner.max_in_flight == 1
    await workspaces.close()


async def test_collection_rejects_foreign_handles_and_early_discard_is_idempotent(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)
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


async def test_runtime_commands_capture_and_remove_managed_output(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)
    provider.root.scripted.extend(
        (
            CommandResult("runtime-result\n", 0, stdout="runtime-result\n"),
            CommandResult("", 0),
            CommandResult("captured", 0, stdout="captured"),
            CommandResult("", 0),
        )
    )
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


async def test_runtime_evaluation_validates_and_normalizes_unavailable_revisions(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)
    provider.root.unavailable_revisions.add("missing")
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


async def test_gate_stages_the_submitted_revision_while_the_live_workspace_changes(
    tmp_path: Path,
) -> None:
    provider = _ContentProvider(tmp_path)

    def implementer_edits() -> None:
        provider.content_root.files["engine.py"] = "v2 (edited while the gate ran)"

    provider.content_root.edit_while_staging = implementer_edits
    runtime = _runtime(provider)
    accuracy = await runtime.evaluation.accuracy("run-a", runtime.workspaces.root, output=DISCARD)

    assert accuracy.result is not None
    assert accuracy.result.passed
    (candidate,) = provider.candidates
    assert candidate.staged == [{"engine.py": "v1"}]
    assert provider.history[accuracy.receipt.revision] == {"engine.py": "v1"}
    assert candidate.accuracy_calls
    assert accuracy.receipt.revision in (candidate.accuracy_calls[0] or "")
    assert provider.root.accuracy_calls == []
    assert candidate.closed
    await runtime.workspaces.close()


async def test_runtime_evaluation_owns_snapshots_receipts_and_command_binding(
    tmp_path: Path,
) -> None:
    provider = _LiveOnlyProvider(tmp_path)
    runtime = _runtime(provider)
    workspace = runtime.workspaces.root
    accuracy = await runtime.evaluation.accuracy(
        "run-a",
        workspace,
        release=True,
        output=DISCARD,
    )
    benchmark = await runtime.evaluation.benchmark(
        workspace,
        required_metrics=frozenset({"throughput"}),
        output=DISCARD,
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
            output=DISCARD,
        )
    # An agent-reported artifact path that is not a workspace file is
    # repairable feedback, including recipe JSON inlined in its place.
    for artifact in (
        "../recipes.json",
        "Accuracy checker: expects cached_tokens > 0 on a repeated prompt",
        "validation/recipes.txt",
        '{"version":1,"recipes":[{"name":"a","command":"python3 -c \\"import re\\nok=True\\""}]}',
    ):
        with pytest.raises(
            LocalValidationRecipeError, match="canonical workspace-relative path"
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


async def test_cancelled_candidate_construction_drains_and_closes_partial_resource(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)
    runner = GatedBlockingRunner(held=True)
    workspaces = _runtime(provider, runner).workspaces
    construction = asyncio.create_task(workspaces.create_candidate())
    await runner.wait_in_flight(1, construction)
    construction.cancel()
    runner.release()
    with pytest.raises(asyncio.CancelledError):
        await construction
    assert len(provider.created) == 1
    assert provider.created[0].closed
    await workspaces.close()


async def test_cancelled_close_keeps_owned_root_cleanup_alive(tmp_path: Path) -> None:
    provider = _Provider(tmp_path)
    root = _Resource(None, tmp_path)
    provider.root = root
    runner = GatedBlockingRunner(held=True)
    workspaces = _runtime(provider, runner).workspaces
    waiter = asyncio.create_task(workspaces.close())
    await runner.wait_in_flight(1, waiter)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    runner.release()
    await workspaces.close()
    assert root.closed
