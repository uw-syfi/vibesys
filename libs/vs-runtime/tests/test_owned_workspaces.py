"""Public composition contracts for runtime-owned workspace resources."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest

from vs_core.api import AdoptionPublication, CaptureCommandBase, CheckoutStatus, CheckoutUpdate
from vs_project.api import adoption_branch
from vs_runtime.api import (
    CaptureStopUnconfirmedError,
    RuntimeContractError,
    member_workspace_id,
)
from vs_runtime.api.core import (
    BaselineEntry,
    CaptureCommandOwner,
    RootBaselineDrift,
    revision_ref,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionScope,
    BlockingOperations,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    TrustedBuild,
    TrustedBuildResult,
    WorkspaceEvaluationSpec,
    WorkspaceHolds,
    WorkspaceRuntime,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeWorkspace,
    MemoryValidationPasses,
)
from vs_sandbox.api import CaptureExecutionIdentity, CaptureExecutionKind, CommandResult
from vs_sim.api.testing import (
    Gate,
    GatedBlockingRunner,
    InlineBlockingRunner,
    SimThreads,
    arrival,
    wait_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import AgentRole
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import BlockingRunner, Event, OutputSink, Threads
from vs_sim.api import DISCARD


class _Resource:
    def __init__(self, identifier: str | None, path: Path, revision: str = "a" * 40) -> None:
        self.id = identifier
        self.path = path
        self.revision: str | None = revision
        self.trusted_input_baseline: str | None = revision
        self.publications: dict[str, AdoptionPublication] = {}
        self.closed = False
        self.detached = False
        self.executions: list[tuple[str, int | None]] = []
        self.capture_executions: list[tuple[str, int | None]] = []
        self.scripted: deque[CommandResult] = deque()
        self.unavailable_revisions: set[str] = set()
        self.accuracy_calls: list[str | None] = []
        self.benchmark_calls: list[tuple[str | None, frozenset[str]]] = []
        self.after_execute: Callable[[str], None] = lambda _command: None

    capture_execution: CaptureExecutionIdentity | None = None

    def publish(self, revision: str, message: str) -> AdoptionPublication:
        if not self.is_retained(revision):
            raise ValueError(revision)
        if revision not in self.publications:
            commit = hashlib.sha1(
                f"{self.revision}:{revision}:{message}".encode(),
                usedforsecurity=False,
            ).hexdigest()
            self.publications[revision] = AdoptionPublication(
                revision=revision,
                commit=commit,
                branch=adoption_branch("owned-workspaces"),
            )
        return self.publications[revision]

    def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate:
        if self.publications.get(publication.revision) != publication:
            raise ValueError(publication.revision)
        self.revision = publication.commit
        return CheckoutUpdate(status=CheckoutStatus.FAST_FORWARDED)

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

    def measured_input(self, revision: str) -> str:
        return f"input:{self.candidate_patch(revision)}"

    def withhold_unread(self) -> None:
        """These resources declare no unread paths."""

    def is_retained(self, revision: str) -> bool:
        return revision not in self.unavailable_revisions

    def trusted_input_changes(self) -> list[str]:
        return []

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def execute(self, command: str, timeout_seconds: int | None) -> CommandResult:
        self.executions.append((command, timeout_seconds))
        result = self.scripted.popleft() if self.scripted else CommandResult("", 0)
        self.after_execute(command)
        return result

    def execute_capture(
        self, command: str, timeout_seconds: int | None, cancel: Event
    ) -> CommandResult:
        del cancel
        self.capture_executions.append((command, timeout_seconds))
        return self.scripted.popleft() if self.scripted else CommandResult("", 0)

    def agent_sandbox(self) -> CommandRunner | None:
        return None

    def agent_scope(self) -> AgentExecutionScope:
        pytest.fail("workspace-only test requested an agent execution scope")

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        return WorkspaceEvaluationSpec(
            build=None,
            accuracy_command="python accuracy.py",
            benchmark_command="python benchmark.py",
            benchmark_contract=None,
            deployment_release_env_var=None,
        )

    async def candidate_build(self, output: OutputSink) -> TrustedBuildResult:
        del output
        return TrustedBuildResult(executed=False, passed=True)

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
        self.unsettled: set[str] = set()
        self.capture_intents: dict[str, CaptureExecutionIdentity] = {}
        self.fail_record = False
        self.fail_clear = False

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

    def unsettled_candidates(self) -> frozenset[str]:
        return frozenset(self.unsettled)

    def record_capture_intent(self, workspace_id: str, execution: CaptureExecutionIdentity) -> None:
        if self.fail_record:
            raise OSError
        if workspace_id in self.capture_intents:
            raise RuntimeError
        self.capture_intents[workspace_id] = execution
        self.unsettled.add(workspace_id)

    def clear_capture_intent(self, workspace_id: str) -> None:
        if self.fail_clear:
            raise OSError
        self.capture_intents.pop(workspace_id, None)
        self.unsettled.discard(workspace_id)

    def reconcile_capture_intents(self) -> frozenset[str]:
        return frozenset(self.unsettled)

    def remove_leftover(self, workspace_id: str, /) -> None:
        del workspace_id


class _LeftoverProvider(_Provider):
    """A previous host's candidate that this provider must not replace or sweep."""

    def __init__(self, path: Path, workspace_id: str, *, reconciled: bool = False) -> None:
        super().__init__(path)
        self.workspace_id = workspace_id
        self.unsettled.add(workspace_id)
        self.capture_intents[workspace_id] = CaptureExecutionIdentity(
            kind=CaptureExecutionKind.SLURM_GPU,
            execution_id=f"capture-{workspace_id}",
        )
        self.reconciled = reconciled
        self.removed: list[str] = []

    def create_candidate(self, workspace_id: str, revision: str) -> _Resource:
        del workspace_id, revision
        pytest.fail("a held unresolved candidate must not be replaced")

    def leftover_candidates(self) -> tuple[str, ...]:
        return (self.workspace_id,)

    def remove_leftover(self, workspace_id: str, /) -> None:
        self.removed.append(workspace_id)

    def reconcile_capture_intents(self) -> frozenset[str]:
        if self.reconciled:
            self.clear_capture_intent(self.workspace_id)
        return frozenset(self.unsettled)


class _OrdinarySessionLeftoverProvider(_Provider):
    """A Docker/host candidate whose session ended with its previous process."""

    def __init__(self, path: Path, workspace_id: str) -> None:
        super().__init__(path)
        self.workspace_id = workspace_id
        self.removed: list[str] = []

    def leftover_candidates(self) -> tuple[str, ...]:
        return () if self.removed else (self.workspace_id,)

    def remove_leftover(self, workspace_id: str, /) -> None:
        self.removed.append(workspace_id)


class _CancellableCaptureResource(_Resource):
    """A capture that settles only after the runtime signals its cancel event."""

    def __init__(self, identifier: str, path: Path, revision: str) -> None:
        super().__init__(identifier, path, revision)
        self.started = Gate()
        self.stopped = Gate()

    def execute(self, command: str, timeout_seconds: int | None) -> CommandResult:
        self.executions.append((command, timeout_seconds))
        if command.startswith("mktemp "):
            return CommandResult("capture-artifact\n", 0, stdout="capture-artifact\n")
        return CommandResult("", 0)

    def execute_capture(
        self, command: str, timeout_seconds: int | None, cancel: Event
    ) -> CommandResult:
        self.capture_executions.append((command, timeout_seconds))
        self.started.open()
        wait_or_fail(cancel, "capture cancellation")
        self.stopped.open()
        return CommandResult("cancelled", 143, stdout="cancelled")


class _SetThenFailEvent:
    """A stop signal that takes effect but whose acknowledgement is lost."""

    def __init__(self, delegate: Event) -> None:
        self._delegate = delegate

    def is_set(self) -> bool:
        return self._delegate.is_set()

    def set(self) -> None:
        self._delegate.set()
        raise _CancelSignalLostError

    def clear(self) -> None:
        self._delegate.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)


class _CancelSignalFailureThreads:
    def __init__(self, delegate: Threads) -> None:
        self._delegate = delegate

    def event(self) -> Event:
        return _SetThenFailEvent(self._delegate.event())

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _CancelSignalLostError(OSError):
    """The cooperative signal took effect but its acknowledgement was lost."""


class _ObservedCancelEvent:
    """An event that exposes when the public cancellation boundary signals it."""

    def __init__(self, delegate: Event, signalled: Gate) -> None:
        self._delegate = delegate
        self._signalled = signalled

    def is_set(self) -> bool:
        return self._delegate.is_set()

    def set(self) -> None:
        self._delegate.set()
        self._signalled.open()

    def clear(self) -> None:
        self._delegate.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)


class _ObservedCancelThreads:
    """Threads fake that makes the runtime's private cancellation signal observable."""

    def __init__(self, delegate: Threads) -> None:
        self._delegate = delegate
        self.signalled = Gate()

    def event(self) -> Event:
        return _ObservedCancelEvent(self._delegate.event(), self.signalled)

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _UnconfirmedCaptureResource(_CancellableCaptureResource):
    """A remote capture whose stop cannot prove the job ended."""

    def execute_capture(
        self, command: str, timeout_seconds: int | None, cancel: Event
    ) -> CommandResult:
        del cancel
        self.capture_executions.append((command, timeout_seconds))
        raise CaptureStopUnconfirmedError


class _CaptureProvider(_Provider):
    def __init__(
        self,
        path: Path,
        *,
        unconfirmed: bool = False,
        durable: bool = False,
        capture_runner: GatedBlockingRunner | None = None,
    ) -> None:
        super().__init__(path)
        self._unconfirmed = unconfirmed
        self._durable = durable
        self._capture_runner = capture_runner
        self.candidate_created = Gate()
        self.capture_prepared = Gate()

    def record_capture_intent(self, workspace_id: str, execution: CaptureExecutionIdentity) -> None:
        super().record_capture_intent(workspace_id, execution)
        if self._capture_runner is not None:
            self._capture_runner.hold()
            self.capture_prepared.open()

    def create_candidate(self, workspace_id: str, revision: str) -> _CancellableCaptureResource:
        resource_type = (
            _UnconfirmedCaptureResource if self._unconfirmed else _CancellableCaptureResource
        )
        resource = resource_type(workspace_id, self.root.path / workspace_id, revision)
        if self._durable:
            resource.capture_execution = CaptureExecutionIdentity(
                kind=CaptureExecutionKind.SLURM_GPU,
                execution_id=f"capture-{workspace_id}",
            )
        self.created.append(resource)
        self.candidate_created.open()
        return resource


class _CaptureRequest(CaptureCommandBase):
    kind: Literal["test.capture"] = "test.capture"


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

    async def candidate_build(self, output: OutputSink) -> TrustedBuildResult:
        del output
        return TrustedBuildResult(executed=False, passed=True)

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


def _runtime(
    provider: _Provider,
    runner: BlockingRunner | None = None,
    threads: Threads | None = None,
) -> WorkspaceRuntime:
    def unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
        pytest.fail("workspace-only test opened an agent execution")

    def unexpected_client(**_kwargs: object) -> AgentClientProtocol:
        pytest.fail("workspace-only test opened an agent client")

    runtime_threads = threads or SimThreads()
    return create_workspace_runtime(
        workspace_resources=provider,
        blocking=BlockingOperations(runner or InlineBlockingRunner(), runtime_threads),
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


async def test_validation_stays_ordinary_while_capture_uses_its_role_and_cleans_up(
    tmp_path: Path,
) -> None:
    provider = _Provider(tmp_path)
    provider.root.scripted.extend(
        (
            CommandResult(
                "API_TOKEN=raw-validation",
                0,
                stdout="API_TOKEN=raw-validation",
                diagnostic_output="API_TOKEN=[REDACTED]",
                diagnostic_stdout="API_TOKEN=[REDACTED]",
            ),
            CommandResult("runtime-result\n", 0, stdout="runtime-result\n"),
            CommandResult("", 0),
            CommandResult(
                "API_TOKEN=raw-captured",
                0,
                stdout="API_TOKEN=raw-captured",
                diagnostic_output="API_TOKEN=[REDACTED]",
                diagnostic_stdout="API_TOKEN=[REDACTED]",
            ),
            CommandResult("", 0),
        )
    )
    runtime = _runtime(provider)
    validated = await runtime.commands.run(
        ("validate",), workspace=runtime.workspaces.root, timeout_seconds=11
    )
    result = await runtime.capture_commands.capture_output(
        ("profiler",),
        workspace=runtime.workspaces.root,
        output_argument="--output",
        timeout_seconds=17,
    )
    assert validated.output == "API_TOKEN=raw-validation"
    assert validated.diagnostic_output == "API_TOKEN=[REDACTED]"
    assert result.output == "API_TOKEN=raw-captured"
    assert result.diagnostic_output == "API_TOKEN=[REDACTED]"
    assert provider.root.executions == [
        ("validate", 11),
        ("mktemp .vibesys-capture.XXXXXXXXXX", None),
        ("cat runtime-result", None),
        ("rm -f runtime-result", None),
    ]
    assert provider.root.capture_executions == [("profiler --output runtime-result", 17)]
    await runtime.workspaces.close()


async def test_public_capture_cancellation_stops_the_worker_before_candidate_cleanup(
    tmp_path: Path,
) -> None:
    """Cancellation is cooperative and the public operation drains before discard."""
    threads = _ObservedCancelThreads(SimThreads())
    runner = GatedBlockingRunner()
    provider = _CaptureProvider(tmp_path, durable=True, capture_runner=runner)
    runtime = _runtime(provider, runner, cast("Threads", threads))
    revision = provider.root.revision or ""
    owner = CaptureCommandOwner(
        runtime.workspaces,
        runtime.capture_commands,
        runtime.workspaces,
        lambda ref: ref.revision_id.root,
    )
    request = _CaptureRequest(
        revision=revision_ref(revision),
        argv=("profiler",),
        output_argument="--output",
        timeout_seconds=60,
    )

    operation = asyncio.create_task(owner.capture(request, "cancelled-operation"))
    await provider.capture_prepared.wait(operation)
    await runner.wait_in_flight(1, operation)
    resource = provider.created[0]
    assert isinstance(resource, _CancellableCaptureResource)
    operation.cancel()
    await threads.signalled.wait(operation)
    runner.release()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert resource.stopped.is_open
    assert resource.executions == [
        ("mktemp .vibesys-capture.XXXXXXXXXX", None),
        ("rm -f capture-artifact", None),
    ]
    assert resource.closed
    await runtime.workspaces.close()


async def test_failed_cancel_signal_keeps_worker_tracking_and_candidate_intent(
    tmp_path: Path,
) -> None:
    """A lost stop acknowledgement cannot clear the candidate's durable ownership."""
    simulator = SimThreads()
    threads = cast("Threads", _CancelSignalFailureThreads(simulator))
    runner = GatedBlockingRunner()
    provider = _CaptureProvider(tmp_path, durable=True, capture_runner=runner)
    runtime = _runtime(provider, runner, threads)
    candidate = await runtime.workspaces.create_candidate(member_id="capture-signal-fault")

    capture = asyncio.create_task(
        runtime.capture_commands.capture_output(
            ("profiler",), workspace=candidate, output_argument="--output"
        )
    )
    await provider.capture_prepared.wait(capture)
    await runner.wait_in_flight(1, capture)
    resource = provider.created[0]
    assert isinstance(resource, _CancellableCaptureResource)

    capture.cancel()
    with pytest.raises(CaptureStopUnconfirmedError):
        await capture

    assert provider.capture_intents
    with pytest.raises(RuntimeContractError, match="unresolved capture"):
        await candidate.discard()

    # The worker remains owned after its caller returns.  Once the boundary is
    # released, it observes the signal that took effect before acknowledgement
    # failed and settles without touching the retained intent.
    runner.release()
    await runtime.workspaces.close()
    assert resource.stopped.is_open
    assert resource.detached
    assert provider.capture_intents


async def test_an_unconfirmed_capture_stop_retains_the_artifact_and_candidate(
    tmp_path: Path,
) -> None:
    threads = SimThreads()
    provider = _CaptureProvider(tmp_path, unconfirmed=True)
    runtime = _runtime(provider, threads=threads)
    runtime.workspaces.follow(lambda: WorkspaceHolds(final=False, attempts=frozenset()))
    revision = provider.root.revision or ""
    owner = CaptureCommandOwner(
        runtime.workspaces,
        runtime.capture_commands,
        runtime.workspaces,
        lambda ref: ref.revision_id.root,
    )
    request = _CaptureRequest(
        revision=revision_ref(revision),
        argv=("profiler",),
        output_argument="--output",
        timeout_seconds=60,
    )

    with pytest.raises(CaptureStopUnconfirmedError):
        await owner.capture(request, "unconfirmed-operation")

    (resource,) = provider.created
    assert resource.executions == [("mktemp .vibesys-capture.XXXXXXXXXX", None)]
    assert not resource.closed
    assert provider.capture_intents == {}
    await runtime.workspaces.close()
    assert resource.detached
    assert not resource.closed


async def test_host_capture_refusal_blocks_reuse_of_its_stable_execution_id(
    tmp_path: Path,
) -> None:
    """A live ordinary-session capture keeps its stable name unavailable to this host."""
    provider = _CaptureProvider(tmp_path, unconfirmed=True)
    runtime = _runtime(provider, threads=SimThreads())
    candidate = await runtime.workspaces.create_candidate(member_id="stable-capture")
    resource = provider.created[0]

    with pytest.raises(CaptureStopUnconfirmedError):
        await runtime.capture_commands.capture_output(
            ("profiler",), workspace=candidate, output_argument="--output"
        )
    with pytest.raises(CaptureStopUnconfirmedError):
        await runtime.capture_commands.capture_output(
            ("profiler",), workspace=candidate, output_argument="--output"
        )

    assert len(resource.capture_executions) == 1
    assert provider.capture_intents == {}
    await runtime.workspaces.close()
    assert resource.detached
    assert not resource.closed


async def test_docker_capture_crash_does_not_permanently_fence_replay(tmp_path: Path) -> None:
    """A dead editor session has no external identity for the next host to reconcile."""
    member_id = "docker-capture-replay"
    workspace_id = member_workspace_id(member_id)
    first_provider = _CaptureProvider(tmp_path / "first", unconfirmed=True)
    first = _runtime(first_provider, threads=SimThreads())
    candidate = await first.workspaces.create_candidate(member_id=member_id)

    with pytest.raises(CaptureStopUnconfirmedError):
        await first.capture_commands.capture_output(
            ("profiler",), workspace=candidate, output_argument="--output"
        )
    await first.workspaces.close()

    assert first_provider.capture_intents == {}
    assert first_provider.created[0].detached

    restarted_provider = _OrdinarySessionLeftoverProvider(tmp_path / "second", workspace_id)
    restarted = _runtime(restarted_provider, threads=SimThreads())
    restarted.workspaces.follow(lambda: WorkspaceHolds(final=False, attempts=frozenset()))
    await restarted.workspaces.release_unheld()
    replay = await restarted.workspaces.create_candidate(member_id=member_id)

    assert restarted_provider.removed == [workspace_id]
    await replay.discard()
    await restarted.workspaces.close()


@pytest.mark.parametrize("failure", ["record", "clear"])
async def test_capture_intent_persistence_failures_retain_the_live_candidate(
    tmp_path: Path, failure: str
) -> None:
    provider = _CaptureProvider(tmp_path, durable=True)
    runtime = _runtime(provider)
    candidate = await runtime.workspaces.create_candidate(member_id="capture-persistence")
    resource = provider.created[0]
    resource.scripted.extend(
        (
            CommandResult("capture-artifact\n", 0, stdout="capture-artifact\n"),
            CommandResult("", 0),
            CommandResult("captured", 0, stdout="captured"),
        )
    )
    if failure == "record":
        provider.fail_record = True
    else:
        provider.fail_clear = True

    with pytest.raises(CaptureStopUnconfirmedError):
        await runtime.capture_commands.capture_output(
            ("profiler",),
            workspace=candidate,
            output_argument="--output",
        )

    assert resource.capture_executions == (
        [] if failure == "record" else [("profiler --output capture-artifact", None)]
    )
    assert not resource.closed
    with pytest.raises(RuntimeContractError, match="unresolved capture"):
        await candidate.discard()
    await runtime.workspaces.close()
    assert resource.detached
    assert not resource.closed


@pytest.mark.parametrize("stage", ["begin", "finish"])
async def test_cancelled_capture_intent_persistence_fails_closed(
    tmp_path: Path, stage: str
) -> None:
    runner = GatedBlockingRunner()
    provider = _Provider(tmp_path)
    runtime = _runtime(provider, runner)
    candidate = await runtime.workspaces.create_candidate(member_id="capture-cancelled-state")
    resource = provider.created[0]
    resource.capture_execution = CaptureExecutionIdentity(
        kind=CaptureExecutionKind.SLURM_GPU,
        execution_id=f"capture-{resource.id}",
    )
    resource.scripted.extend(
        (
            CommandResult("capture-artifact\n", 0, stdout="capture-artifact\n"),
            CommandResult("", 0),
            CommandResult("captured", 0, stdout="captured"),
        )
    )
    persistence_reached = Gate()

    def hold_before_persistence(command: str) -> None:
        expected = command.startswith("mktemp ") if stage == "begin" else command.startswith("cat ")
        if expected:
            runner.hold()
            persistence_reached.open()

    resource.after_execute = hold_before_persistence
    capture = asyncio.create_task(
        runtime.capture_commands.capture_output(
            ("profiler",), workspace=candidate, output_argument="--output"
        )
    )
    await persistence_reached.wait(capture)
    await runner.wait_in_flight(1, capture)

    capture.cancel()
    runner.release()
    with pytest.raises(CaptureStopUnconfirmedError):
        await capture

    assert not resource.closed
    with pytest.raises(RuntimeContractError, match="unresolved capture"):
        await candidate.discard()
    await runtime.workspaces.close()
    assert resource.detached
    assert not resource.closed


async def test_a_restart_neither_sweeps_nor_replaces_unresolved_capture_scratch(
    tmp_path: Path,
) -> None:
    identity = "unresolved-operation"
    member_id = f"capture-{identity[:32]}"
    workspace_id = member_workspace_id(member_id)
    provider = _LeftoverProvider(tmp_path, workspace_id)
    runtime = _runtime(provider)
    runtime.workspaces.follow(lambda: WorkspaceHolds(final=False, attempts=frozenset()))

    await runtime.workspaces.release_unheld()
    with pytest.raises(RuntimeContractError, match="retained by the previous host"):
        await runtime.workspaces.create_candidate("revision", member_id=member_id)

    assert provider.removed == []
    await runtime.workspaces.close()


async def test_restart_sweeps_capture_only_after_external_settlement_is_confirmed(
    tmp_path: Path,
) -> None:
    workspace_id = member_workspace_id("capture-settled-operation")
    provider = _LeftoverProvider(tmp_path, workspace_id, reconciled=True)
    runtime = _runtime(provider)
    runtime.workspaces.follow(lambda: WorkspaceHolds(final=False, attempts=frozenset()))

    await runtime.workspaces.release_unheld()

    assert provider.removed == [workspace_id]
    assert provider.capture_intents == {}


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


_BUILD = TrustedBuild(command="make lib.so", outputs=("out/lib.so",))

type _Produced = bytes | Path
"""What a fake build leaves at a path: file contents, or a symlink to the given path."""


class _BuildingResource(_Resource):
    """A workspace whose confined build leaves ``produces`` in its checkout."""

    def __init__(
        self, identifier: str | None, path: Path, revision: str, produces: dict[str, _Produced]
    ) -> None:
        super().__init__(identifier, path, revision)
        path.mkdir(parents=True, exist_ok=True)
        self.produces = produces
        self.builds = 0

    def snapshot(self, label: str) -> str:
        del label
        return self.revision or "a" * 40

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        return WorkspaceEvaluationSpec(
            build=_BUILD,
            accuracy_command="python accuracy.py",
            benchmark_command="python benchmark.py",
            benchmark_contract=None,
            deployment_release_env_var=None,
        )

    async def candidate_build(self, output: OutputSink) -> TrustedBuildResult:
        del output
        self.builds += 1
        for relative, produced in self.produces.items():
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(produced, Path):
                target.symlink_to(produced)
            else:
                target.write_bytes(produced)
        return TrustedBuildResult(command=_BUILD.command, executed=True, passed=True)


class _BuildingProvider(_Provider):
    def __init__(self, path: Path, produces: dict[str, _Produced]) -> None:
        super().__init__(path)
        self.produces = produces
        self.live = _BuildingResource(None, path / "root", "a" * 40, produces)
        self.root = self.live
        self.building: list[_BuildingResource] = []

    def create_candidate(self, workspace_id: str, revision: str) -> _BuildingResource:
        resource = _BuildingResource(
            workspace_id, self.root.path / workspace_id, revision, self.produces
        )
        self.created.append(resource)
        self.building.append(resource)
        return resource


async def test_a_commit_builds_once_and_its_checks_load_what_that_build_produced(
    tmp_path: Path,
) -> None:
    provider = _BuildingProvider(tmp_path, {"out/lib.so": b"\x7fELF candidate"})
    runtime = _runtime(provider)
    workspace = runtime.workspaces.root

    first = await runtime.evaluation.build(workspace, output=DISCARD)
    again = await runtime.evaluation.build(workspace, output=DISCARD)
    await runtime.evaluation.accuracy("run-a", workspace, output=DISCARD)
    await runtime.evaluation.benchmark(workspace, required_metrics=frozenset(), output=DISCARD)

    assert (first.passed, first.reused) == (True, False)
    assert (again.passed, again.reused) == (True, True)
    # The build ran once, in a fresh checkout of the commit, never in the live workspace.
    assert provider.live.builds == 0
    builder, checker, measurer = provider.building
    assert (builder.builds, checker.builds, measurer.builds) == (1, 0, 0)
    for gate in (checker, measurer):
        assert (gate.path / "out/lib.so").read_bytes() == b"\x7fELF candidate"
    commands = (checker.accuracy_calls[0], measurer.benchmark_calls[0][0])
    for command in commands:
        assert command is not None
        assert "VIBESYS_CANDIDATE_OUTPUTS=out/lib.so" in command


@pytest.mark.parametrize(
    "produces",
    [{}, {"out/other.so": b"x"}, {"out/lib.so/inner": b"x"}, {"out/lib.so": Path("/etc/hosts")}],
    ids=["nothing", "another-file", "a-directory", "a-symlink"],
)
async def test_a_build_that_misses_a_required_output_fails_naming_it(
    tmp_path: Path, produces: dict[str, _Produced]
) -> None:
    provider = _BuildingProvider(tmp_path, produces)
    runtime = _runtime(provider)
    workspace = runtime.workspaces.root

    built = await runtime.evaluation.build(workspace, output=DISCARD)

    assert not built.passed
    assert built.missing_output is not None
    assert "out/lib.so" in built.missing_output
    # Nothing was recorded, so a check of the commit refuses to run without its build.
    with pytest.raises(RuntimeContractError, match="no verified build"):
        await runtime.evaluation.accuracy("run-a", workspace, output=DISCARD)
