"""The production workspace owner behind typed replay, over real retained Git content."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api import RevisionRef
from vs_core.api.replay import ReplayCall, ReplayFlow, ReplaySource, SemanticKey, classify_context
from vs_project.api import FakeStateStore, StateNamespace
from vs_proto.api import Ended, EpochStarted, Outcome, core_leaves, replay
from vs_proto.api import replay_workspace as pb
from vs_runtime.api.core import ReceiptStore
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.executor_contracts import FaultingNamespace, ProcessKilledError
from vs_runtime.api.replay import (
    ReplayAbsent,
    ReplayCompleted,
    ReplayEnding,
    ReplayLaunch,
    ReplayOperationError,
    ReplayOperationHost,
    ReplayRunHost,
    ReplayRuntime,
    ReplayUnknown,
    WorkspaceReplayExecutor,
    drive_replay,
)
from vs_sim.api.testing import SimThreads, swept

from .test_workspace_requests import _candidate_path, _commits, _env, _executor

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

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


@pytest.mark.parametrize(
    "mode", [pb.ReplayWorkspaceMode.ISOLATED, pb.ReplayWorkspaceMode.EXCLUSIVE_ROOT]
)
@pytest.mark.parametrize("effect", range(5))
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
def test_real_workspace_replay_rebinds_and_preserves_content_at_every_effect_boundary(
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

        def host(*, restart: bool = False) -> ReplayRunHost:
            resources = env.start_host() if restart else initial
            owner, receipts = _executor(resources)
            executor = WorkspaceReplayExecutor(
                owner,
                receipts,
                ReplayOperationHost(env.run_id, store, sim.clock),
                within_seconds=100,
            )
            return ReplayRunHost(
                ReplayRuntime(store, root, replay.ReplayAllowance(iterations=1)),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                ),
            )

        async def scenario() -> None:
            first = host()
            first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
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


def test_workspace_recovery_sweeps_real_owner_receipt_writes(tmp_path: Path, sim: Sim) -> None:
    def run_case(case_root: Path, crash_at: int | None) -> int:
        case_root.mkdir()
        with _env(case_root) as env:
            initial = env.hosts[0]
            namespace = env.project.state.local_namespace(env.run_id, "receipts")
            faulting = FaultingNamespace(namespace, crash_at)
            faulted_receipts = ReceiptStore(cast("StateNamespace", faulting), SimThreads())
            store = FakeStateStore()

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
                    )
                    return ReplayRunHost(
                        ReplayRuntime(store, root, replay.ReplayAllowance(iterations=1)),
                        executor,
                        sim.clock,
                        lambda _: ReplayEnding(
                            Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
                        ),
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
                assert (initial.root.path / "candidate.py").read_text() == "VALUE = 2\n"
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
            assert store.release(fence, sim.clock.now())
            assert isinstance(await executor.inspect(span, fence), ReplayUnknown)
            with pytest.raises(ReplayOperationError, match="lease was lost"):
                await executor.execute(span, fence)

        sim.run(scenario())
