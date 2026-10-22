"""The production workspace owner behind typed replay, over real retained Git content."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api import AttemptId, AttemptRef, EnsureWorkspace, RevisionRef
from vs_core.api.replay import ReplayCall, ReplayFlow, ReplaySource, SemanticKey, classify_context
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import FakeStateStore, StateNamespace, adoption_branch
from vs_proto.api import (
    Ended,
    EpochStarted,
    Outcome,
    core_agent_facts,
    core_common,
    core_leaves,
    core_vocabulary,
    replay,
)
from vs_proto.api import replay_workspace as pb
from vs_runtime.api.core import ReceiptStore, StoreWorkspaceReceipts
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.executor_contracts import FaultingNamespace, ProcessKilledError
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayAbsent,
    ReplayCompleted,
    ReplayEnding,
    ReplayExecutors,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    ReplayUnknown,
    WorkspaceReplayExecutor,
    WriterProofError,
    WriterRegistry,
    drive_replay,
)
from vs_sim.api.testing import SimThreads, swept

from .test_replay_physical import physical_root
from .test_workspace_requests import _candidate_path, _commits, _env, _executor, _git
from .test_writer_reconciliation import empty_barrier

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from pydantic import BaseModel

    from vs_core.api import RequestBase
    from vs_runtime.api.core import ExecutionContext, ExecutionOutcome
    from vs_sim.api.testing import Sim


def step(
    ctx: ReplayFlow, request: pb.ReplayWorkspaceRequest
) -> ReplaySource[pb.ReplayWorkspaceResult]:
    assert request.action is not None
    operation = "workspace." + request.action.field
    return ctx.call(
        ReplayCall(
            SemanticKey(operation).item("round-1"),
            classify_context(request, contract="workspace.v1"),
            "workspace.v1",
            replay.ReplayPolicy(
                reply_type=pb.ReplayWorkspaceResult.desc().type_name,
                reservation=replay.ReplayResources(money_usd=0, input_tokens=0, output_tokens=0),
            ),
            pb.ReplayWorkspaceResult,
        )
    )


def request(action: Oneof) -> pb.ReplayWorkspaceRequest:
    return pb.ReplayWorkspaceRequest(attempt="round-1", admission="admit-1", action=action)


def program(
    base: core_leaves.RevisionRef, mode: pb.ReplayWorkspaceMode
) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
    async def root(ctx: ReplayFlow) -> object:
        opened = await step(
            ctx, request(Oneof("open", pb.ReplayOpenWorkspace(mode=mode, base=base)))
        )
        assert opened.status is pb.ReplayWorkspaceStatus.SUCCEEDED
        snapshot = await step(
            ctx,
            request(
                Oneof("snapshot", pb.ReplaySnapshotWorkspace(retention=pb.ReplayRetention.WIP))
            ),
        )
        assert snapshot.status is pb.ReplayWorkspaceStatus.SUCCEEDED
        assert snapshot.revision is not None
        retained = await step(
            ctx,
            request(
                Oneof(
                    "retain",
                    pb.ReplayRetainWorkspace(
                        revision=snapshot.revision, retention=pb.ReplayRetention.CANDIDATE
                    ),
                )
            ),
        )
        assert retained.status is pb.ReplayWorkspaceStatus.SUCCEEDED
        settled = await step(ctx, request(Oneof("settle", pb.ReplaySettleWorkspace())))
        assert settled.status is pb.ReplayWorkspaceStatus.SUCCEEDED
        assert settled.released
        return await step(
            ctx,
            pb.ReplayWorkspaceRequest(
                action=Oneof(
                    "adopt",
                    pb.ReplayAdoptWorkspace(
                        revision=snapshot.revision, settlement_id="settlement-1"
                    ),
                )
            ),
        )

    return root


def launch(name: str) -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="workspace-requests"), name, "host-a", 100)


def workspace_boundary_case(
    tmp_path: Path, sim: Sim, mode: pb.ReplayWorkspaceMode, effect: int, boundary: str
) -> None:
    with _env(tmp_path) as env:
        initial = env.hosts[0]
        commit = initial.root.revision
        assert commit is not None
        base = to_proto(RevisionRef.of_git_commit(commit))
        assert isinstance(base, core_leaves.RevisionRef)
        root: Callable[[ReplayFlow], Coroutine[object, object, object]] = program(base, mode)
        store = FakeStateStore()
        barrier = empty_barrier(env.project)

        def host(*, restart: bool = False) -> ReplayRunHost:
            resources = env.start_host() if restart else initial
            owner, receipts = _executor(resources)
            executor = WorkspaceReplayExecutor(
                owner,
                receipts,
                ReplayOperationHost(env.run_id, store, sim.clock),
                within_seconds=100,
                writers=barrier.registry,
            )
            return ReplayRunHost(
                ReplayRuntime(store, root, replay.ReplayAllowance(iterations=1)),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                ),
                writer_barrier=barrier,
            )

        async def scenario() -> None:
            first = host()
            first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
            await barrier.reconcile(first.runtime, now=sim.clock.now(), at=Timestamp())
            for ordinal in range(effect + 1):
                (span,) = first.runtime.prepare(now=sim.clock.now(), at=Timestamp())
                if ordinal == effect and boundary == "prepared":
                    break
                result = await first.runtime.dispatch(span.id, first.executor, now=sim.clock.now())
                if ordinal == 0:
                    target = (
                        _candidate_path(initial)
                        if mode is pb.ReplayWorkspaceMode.ISOLATED
                        else initial.root.path
                    )
                    (target / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
                if ordinal == effect and boundary == "executed":
                    break
                first.runtime.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
                if ordinal == effect and boundary == "observed":
                    break
                first.runtime.accept(span.id, now=sim.clock.now(), at=Timestamp())
            first.runtime.close(now=sim.clock.now())
            resumed = host(restart=True)
            result = await drive_replay(resumed, launch("resume"))
            assert isinstance(result, ReplayCompleted)
            assert isinstance(result.value, pb.ReplayWorkspaceResult)
            assert result.value.status is pb.ReplayWorkspaceStatus.SUCCEEDED
            assert initial.root.path.joinpath("candidate.py").read_text() == (
                "VALUE = 1\n" if effect == 0 and boundary == "prepared" else "VALUE = 2\n"
            )
            commits = _commits(initial)
            assert await drive_replay(host(restart=True), launch("again")) == result
            assert _commits(initial) == commits

        sim.run(scenario())


@pytest.mark.parametrize(
    "mode", [pb.ReplayWorkspaceMode.ISOLATED, pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT]
)
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
def test_real_workspace_replay_rebinds_and_preserves_content_at_every_effect_boundary(
    tmp_path_factory: pytest.TempPathFactory, sim: Sim, mode: pb.ReplayWorkspaceMode, boundary: str
) -> None:
    # Every boundary runs on a PR; nightly depth sweeps every effect as well.
    for effect in swept(5, sample=1):
        workspace_boundary_case(
            tmp_path_factory.mktemp(f"workspace-{effect}"), sim, mode, effect, boundary
        )


def test_workspace_recovery_sweeps_real_owner_receipt_writes(tmp_path: Path, sim: Sim) -> None:
    def run_case(case_root: Path, crash_at: int | None) -> int:
        case_root.mkdir()
        with _env(case_root) as env:
            initial = env.hosts[0]
            namespace = env.project.state.local_namespace(env.run_id, "receipts")
            faulting = FaultingNamespace(namespace, crash_at)
            faulted_receipts = ReceiptStore(cast("StateNamespace", faulting), SimThreads())
            store = FakeStateStore()
            barrier = empty_barrier(env.project)

            async def scenario() -> None:
                baseline = initial.root.revision
                assert baseline is not None
                (initial.root.path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
                seed = await initial.root.snapshot_and_retain("seed", retention_label="candidate")
                await initial.root.restore(baseline)
                base = to_proto(RevisionRef.of_git_commit(seed))
                assert isinstance(base, core_leaves.RevisionRef)
                root = program(base, pb.ReplayWorkspaceMode.ISOLATED)

                def host(*, restarting: bool = False) -> ReplayRunHost:
                    resources = env.start_host() if restarting else initial
                    receipts = env.store() if restarting else faulted_receipts
                    owner, _ = _executor(resources, receipts)
                    executor = WorkspaceReplayExecutor(
                        owner,
                        receipts,
                        ReplayOperationHost(env.run_id, store, sim.clock),
                        within_seconds=100,
                        writers=barrier.registry,
                    )
                    return ReplayRunHost(
                        ReplayRuntime(store, root, replay.ReplayAllowance(iterations=1)),
                        executor,
                        sim.clock,
                        lambda _: ReplayEnding(
                            Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                        ),
                        writer_barrier=barrier,
                    )

                if crash_at is None:
                    result = await drive_replay(host(), launch("first"))
                else:
                    with pytest.raises(ProcessKilledError):
                        await drive_replay(host(), launch("first"))
                    result = await drive_replay(host(restarting=True), launch("resume"))
                assert isinstance(result, ReplayCompleted)
                assert isinstance(result.value, pb.ReplayWorkspaceResult)
                assert result.value.status is pb.ReplayWorkspaceStatus.SUCCEEDED
                # Restoring the baseline leaves the run branch dirty, so publication
                # records the candidate and the checkout stays untouched.
                assert (initial.root.path / "candidate.py").read_text() == "VALUE = 1\n"
                assert (
                    _git(
                        initial.root.path,
                        "show",
                        f"{adoption_branch(env.run_id)}:candidate.py",
                    )
                    == "VALUE = 2"
                )
                commits = _commits(initial)
                assert await drive_replay(host(restarting=True), launch("again")) == result
                assert _commits(initial) == commits

            sim.run(scenario())
            return faulting.writes

    writes = run_case(tmp_path / "baseline", None)
    for boundary in swept(2 * writes, sample=3):
        run_case(tmp_path / f"crash-{boundary}", boundary)


@pytest.mark.parametrize("executed", [False, True])
def test_workspace_inspection_fences_absence_and_preserves_physical_payload(
    tmp_path: Path, sim: Sim, *, executed: bool
) -> None:
    with _env(tmp_path) as env:
        resources = env.hosts[0]
        revision = resources.root.revision
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        store = FakeStateStore()
        owner, receipts = _executor(resources)
        executor = WorkspaceReplayExecutor(
            owner, receipts, ReplayOperationHost(env.run_id, store, sim.clock), within_seconds=100
        )
        runtime = ReplayRuntime(
            store, program(base, pb.ReplayWorkspaceMode.ISOLATED), replay.ReplayAllowance()
        )

        async def scenario() -> None:
            runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
            (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
            runtime.close(now=sim.clock.now())
            fence = store.acquire("host-a", sim.clock.now(), 100)
            assert fence is not None
            if executed:
                original = await executor.execute(span, fence)
                assert await executor.execute(span, fence) == original
                assert await executor.inspect(span, fence) == original
                assert span.replay is not None
                assert span.replay.policy is not None
                assert span.replay.policy.request is not None
                altered = pb.ReplayWorkspaceRequest.from_binary(span.replay.policy.request.value)
                altered.admission = "another-admission"
                span.replay.policy.request.value = altered.to_binary()
                for action in (executor.execute, executor.inspect):
                    with pytest.raises(ReplayOperationError, match="payload conflict"):
                        await action(span, fence)
            else:
                assert await executor.inspect(span, fence) == ReplayAbsent()
                with pytest.raises(ReplayOperationError, match="withdrawn"):
                    await executor.execute(span, fence)
                assert await executor.inspect(span, fence) == ReplayAbsent()
            assert store.release(fence, sim.clock.now())
            assert isinstance(await executor.inspect(span, fence), ReplayUnknown)
            with pytest.raises(ReplayOperationError, match="lease was lost"):
                await executor.execute(span, fence)

        sim.run(scenario())


@pytest.mark.parametrize("with_receipt", [False, True])
@pytest.mark.parametrize("protect_adoption", [False, True])
def test_adoption_consumes_native_evidence_without_fabricating_legacy_observations(
    tmp_path: Path, sim: Sim, *, with_receipt: bool, protect_adoption: bool
) -> None:
    with _env(tmp_path) as env:
        resources = env.hosts[0]
        revision = resources.root.trusted_input_baseline
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        scope = core_common.Scope(
            owner=core_common.ScopeOwner(
                owner=Oneof("run", core_common.RunId(root=env.run_id, kind="run"))
            ),
            generation="0",
        )
        source = core_common.RequestId(root="measurement-1", kind="request")
        evidence = core_agent_facts.EvidenceRef(
            evidence_id=core_common.EvidenceId(root="evidence-1", kind="evidence"),
            kind=core_vocabulary.EvidenceKind.CORRECTNESS,
            purpose=core_agent_facts.EvidencePurpose.OFFICIAL,
            scope=scope,
            source_request=source,
            candidate=base,
            observation_sequence="1",
            evaluator_digest="evaluator",
            workload_digest="workload",
            environment_digest="environment",
            provenance=core_agent_facts.EvidenceProvenance.TRUSTED,
            status=core_vocabulary.ObservationStatus.SUCCEEDED,
            acceptance_receipt=None
            if not with_receipt
            else core_agent_facts.EvidenceAcceptanceReceipt(
                source_request=source,
                scope=scope,
                observation_sequence="1",
                status=core_vocabulary.ObservationStatus.SUCCEEDED,
            ),
        )
        asked = pb.ReplayWorkspaceRequest(
            action=Oneof(
                "adopt", pb.ReplayAdoptWorkspace(revision=base, baseline=True, evidence=[evidence])
            )
        )

        async def root(ctx: ReplayFlow) -> object:
            return await step(ctx, asked)

        owner, receipts = _executor(resources)
        if protect_adoption:
            owner.protect_adoption()
            (resources.root.path / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
            (resources.root.path / "operator-note.txt").write_text("Keep this.\n", encoding="utf-8")
        original_commits = _commits(resources)
        barrier = empty_barrier(env.project)
        store = FakeStateStore()
        executor = WorkspaceReplayExecutor(
            owner,
            receipts,
            ReplayOperationHost(env.run_id, store, sim.clock),
            within_seconds=100,
            writers=barrier.registry,
        )
        runtime = ReplayRuntime(store, root, replay.ReplayAllowance())

        async def scenario() -> None:
            run_host = ReplayRunHost(
                runtime,
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                ),
                writer_barrier=barrier,
            )
            completed = await drive_replay(run_host, launch("first"))
            assert isinstance(completed, ReplayCompleted)
            assert isinstance(completed.value, pb.ReplayWorkspaceResult)
            assert completed.value.status is (
                pb.ReplayWorkspaceStatus.REJECTED
                if protect_adoption
                else pb.ReplayWorkspaceStatus.SUCCEEDED
            )
            if protect_adoption:
                assert (resources.root.path / "candidate.py").read_text() == "VALUE = 9\n"
                assert (resources.root.path / "operator-note.txt").read_text() == "Keep this.\n"
                assert _commits(resources) == original_commits
            (span,) = runtime.archive.spans
            assert span.replay is not None
            assert span.replay.policy is not None
            assert span.replay.policy.request is not None
            recorded = pb.ReplayWorkspaceRequest.from_binary(span.replay.policy.request.value)
            assert recorded == asked

        sim.run(scenario())


class AcquisitionNamespace:
    """Forward real acquisition receipts, with a lost durable-intent reply at W9a."""

    def __init__(self, namespace: StateNamespace, gate: FaultGate) -> None:
        self.namespace, self.gate = namespace, gate

    def save(self, relative_path: str, model: BaseModel) -> None:
        self.gate.around(
            Boundary.DURABLE_WRITE, "W9a", partial(self.namespace.save, relative_path, model)
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self.namespace, name)


@pytest.mark.parametrize("row", ["W9a", "W9b"])
def test_backing_acquisition_crash_reconciles_intent_and_installed_content(
    tmp_path: Path, sim: Sim, row: str
) -> None:
    with _env(tmp_path) as env:
        initial = env.hosts[0]
        baseline = initial.root.revision
        assert baseline is not None
        base = to_proto(RevisionRef.of_git_commit(baseline))
        assert isinstance(base, core_leaves.RevisionRef)
        store = FakeStateStore()
        barrier = empty_barrier(env.project)
        gate = FaultGate(
            FaultPlan(
                seed=0,
                rules=(
                    FaultRule(
                        boundary=Boundary.DURABLE_WRITE
                        if row == "W9a"
                        else Boundary.EXECUTOR_REQUEST,
                        target=row,
                        at=1,
                        fault=HostFault.CRASH_AFTER,
                    ),
                ),
            )
        )

        async def root(ctx: ReplayFlow) -> object:
            return await step(
                ctx,
                request(
                    Oneof(
                        "open",
                        pb.ReplayOpenWorkspace(
                            mode=pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT,
                            base=base,
                        ),
                    )
                ),
            )

        def binding(*, restarted: bool = False) -> ReplayRunHost:
            namespace = env.project.state.local_namespace(env.run_id, "receipts")
            receipts = ReceiptStore(
                cast("StateNamespace", AcquisitionNamespace(namespace, gate)), SimThreads()
            )
            resources = env.start_host() if restarted else initial
            owner, _ = _executor(resources, receipts)
            executor = WorkspaceReplayExecutor(
                owner,
                receipts,
                ReplayOperationHost(env.run_id, store, sim.clock),
                within_seconds=100,
                writers=barrier.registry,
            )
            return ReplayRunHost(
                ReplayRuntime(store, root, replay.ReplayAllowance()),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Acquired.", resumable=True)
                ),
                writer_barrier=barrier,
            )

        async def scenario() -> None:
            first = binding()
            first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
            await barrier.reconcile(first.runtime, now=sim.clock.now(), at=Timestamp())
            (span,) = first.runtime.prepare(now=sim.clock.now(), at=Timestamp())
            try:
                with pytest.raises(HostCrashError, match=row):
                    await gate.around_async(
                        Boundary.EXECUTOR_REQUEST,
                        "W9b",
                        partial(
                            first.runtime.dispatch, span.id, first.executor, now=sim.clock.now()
                        ),
                    )
            finally:
                first.runtime.close(now=sim.clock.now())
            gate.heal()
            resumed = binding(restarted=True)
            result = await drive_replay(resumed, launch("resume"))
            assert isinstance(result, ReplayCompleted)
            assert isinstance(result.value, pb.ReplayWorkspaceResult)
            assert result.value.status is pb.ReplayWorkspaceStatus.SUCCEEDED
            assert (initial.root.path / "candidate.py").read_text() == "VALUE = 1\n"
            assert barrier.registry.require_proof().manifest is not None
            assert resumed.runtime.archive.unresolved == ()
            assert await drive_replay(binding(restarted=True), launch("again")) == result

        sim.run(scenario())


@pytest.mark.regression
def test_old_empty_proof_cannot_release_root_after_new_physical_writers(
    tmp_path: Path, sim: Sim
) -> None:
    with _env(tmp_path) as env:
        resources = env.hosts[0]
        revision = resources.root.revision
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        store = FakeStateStore()
        barrier = empty_barrier(env.project)
        owner, receipts = _executor(resources)
        workspace = WorkspaceReplayExecutor(
            owner,
            receipts,
            ReplayOperationHost(env.run_id, store, sim.clock),
            within_seconds=100,
            writers=barrier.registry,
        )
        writer = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="done")))
        executor = ReplayExecutors(
            {pb.ReplayWorkspaceRequest: workspace, core_common.Identity: writer}
        )

        async def root(ctx: ReplayFlow) -> object:
            await step(
                ctx,
                request(
                    Oneof(
                        "open",
                        pb.ReplayOpenWorkspace(
                            mode=pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT, base=base
                        ),
                    )
                ),
            )
            await physical_root(ctx)
            return await step(ctx, request(Oneof("settle", pb.ReplaySettleWorkspace())))

        async def scenario() -> None:
            binding = ReplayRunHost(
                ReplayRuntime(store, root, replay.ReplayAllowance()),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                ),
                writer_barrier=barrier,
            )
            with pytest.raises(ValueError, match="does not cover"):
                await drive_replay(binding, launch("first"))
            assert len(writer.invocations) == 1
            assert StoreWorkspaceReceipts(receipts).root_holder() == AttemptRef(
                attempt_id=AttemptId(root="round-1"), generation=0
            )

        sim.run(scenario())


def test_root_acquisition_holds_backing_exclusion_across_owner_awaits(
    tmp_path: Path, sim: Sim
) -> None:
    with _env(tmp_path) as env:
        resources = env.hosts[0]
        revision = resources.root.revision
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        store = FakeStateStore()
        barrier = empty_barrier(env.project)
        backing = barrier.registry.backing
        backing.generation += 1
        successor = WriterRegistry(
            env.project.state.state_store_namespace("backing-a"), backing, SimThreads()
        )
        owner, receipts = _executor(resources)

        class CheckedAcquisition:
            async def execute(
                self, request: RequestBase, context: ExecutionContext
            ) -> ExecutionOutcome:
                assert isinstance(request, EnsureWorkspace)
                await sim.clock.sleep(1)
                with pytest.raises(WriterProofError, match="held by another"):
                    successor.create()
                return await owner.execute(request, context)

        async def root(ctx: ReplayFlow) -> object:
            return await step(
                ctx,
                request(
                    Oneof(
                        "open",
                        pb.ReplayOpenWorkspace(
                            mode=pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT, base=base
                        ),
                    )
                ),
            )

        async def scenario() -> None:
            executor = WorkspaceReplayExecutor(
                CheckedAcquisition(),
                receipts,
                ReplayOperationHost(env.run_id, store, sim.clock),
                within_seconds=100,
                writers=barrier.registry,
            )
            binding = ReplayRunHost(
                ReplayRuntime(store, root, replay.ReplayAllowance()),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Acquired.", resumable=True)
                ),
                writer_barrier=barrier,
            )
            result = await drive_replay(binding, launch("first"))
            assert isinstance(result, ReplayCompleted)
            assert isinstance(result.value, pb.ReplayWorkspaceResult)
            assert result.value.status is pb.ReplayWorkspaceStatus.SUCCEEDED
            successor.create()
            with pytest.raises(WriterProofError, match="another active"):
                barrier.registry.require_proof()

        sim.run(scenario())
