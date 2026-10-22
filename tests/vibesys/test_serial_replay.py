"""Native serial effects use the production roadmap and capture owners under replay."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from protobuf import Message
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vibesys.orchestration.single.search.api import OPERATIONS, CaptureCommand
from vibesys.serial_replay import serial_replay_bindings
from vibesys.serial_roadmap import RoadmapOwner
from vs_core.api import CommandCapture, OperationRegistry, RevisionRef
from vs_core.api.replay import ReplayCall, ReplayFlow, ReplaySource, SemanticKey, classify_context
from vs_issue_tracker.api import (
    DocumentRoadmap,
    FakeRoadmap,
    Roadmap,
    RoadmapItemsPut,
    RoadmapSnapshot,
    roadmap_operation_registrations,
)
from vs_project.api import FakeStateStore, Project
from vs_proto.api import (
    Ended,
    EpochStarted,
    Outcome,
    core_leaves,
    replay,
)
from vs_proto.api import (
    serial_effects as pb,
)
from vs_runtime.api import CommandResult
from vs_runtime.api.core import (
    CaptureCommandOwner,
    OperationCatalog,
    OperationEntry,
    OperationRole,
    ReceiptStore,
    operation_roles,
)
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.replay import (
    RegisteredReplayExecutor,
    ReplayCompleted,
    ReplayEnding,
    ReplayLaunch,
    ReplayOperationHost,
    ReplayParked,
    ReplayRunHost,
    ReplayRuntime,
    run_replay,
)
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import Sim, SimThreads

COMMIT = "a" * 40
type Root = Callable[[ReplayFlow], Coroutine[object, object, object]]


def call[T: Message](
    ctx: ReplayFlow, operation: str, request: Message, reply: type[T], *, item: str = "round-1"
) -> ReplaySource[T]:
    return ctx.call(
        ReplayCall(
            SemanticKey(operation).item(item),
            classify_context(request, contract=f"{operation}.v1"),
            f"{operation}.v1",
            replay.ReplayPolicy(
                reply_type=reply.desc().type_name,
                reservation=replay.ReplayResources(money_usd=0, input_tokens=0, output_tokens=0),
            ),
            reply,
        )
    )


async def roadmap_calls(ctx: ReplayFlow) -> tuple[pb.SerialRoadmapPut, pb.SerialRoadmapRead]:
    item = pb.SerialRoadmapItem(
        id="major-1",
        kind=pb.RoadmapKind.MAJOR,
        title="Reduce calls",
        status=pb.RoadmapProgress.TODO,
    )
    put = await call(
        ctx, "roadmap.put_items", pb.PutSerialRoadmap(items=[item]), pb.SerialRoadmapPut
    )
    read = await call(ctx, "roadmap.read", pb.ReadSerialRoadmap(), pb.SerialRoadmapRead)
    return put, read


async def root(ctx: ReplayFlow) -> object:
    put, read = await roadmap_calls(ctx)
    revision = to_proto(RevisionRef.of_git_commit(COMMIT))
    assert isinstance(revision, core_leaves.RevisionRef)
    capture = pb.CaptureSerialAttribution(
        revision=revision,
        argv=["profile", "--fast"],
        output_argument="--output",
        timeout_seconds=10,
    )
    captured = await call(ctx, "attribution.capture", capture, pb.SerialAttributionCaptured)
    return put, read, captured


@dataclass(frozen=True)
class World:
    compose: Callable[[Root, float], ReplayRunHost]
    commands: FakeCommands
    workspaces: FakeWorkspaces
    roadmap: Roadmap

    def host(self, program: Root = root, *, within_seconds: float = 30) -> ReplayRunHost:
        return self.compose(program, within_seconds)


def world(tmp_path: Path, sim: Sim, storage: str) -> World:
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    roadmap = FakeRoadmap() if storage == "fake" else DocumentRoadmap(namespace)
    roadmap_owner = RoadmapOwner(roadmap)
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=tmp_path / "workspace", known_revisions={COMMIT}),
        supports_parallel_candidates=True,
    )
    commands = FakeCommands()
    commands.script(CommandResult(output="profile result", exit_code=0))
    capture_owner = CaptureCommandOwner(
        workspaces, commands, workspaces, lambda ref: ref.git_commit
    )
    registration = operation_roles(OPERATIONS)[OperationRole.CAPTURE_COMMAND]
    entries = (
        *(OperationEntry.owned(item, roadmap_owner) for item in roadmap_operation_registrations()),
        OperationEntry.owned(registration, capture_owner),
    )
    catalog = OperationCatalog(
        OperationRegistry(tuple(entry.registration for entry in entries)), entries
    )
    store = FakeStateStore()

    def host(program: Root, within: float) -> ReplayRunHost:
        executor = RegisteredReplayExecutor(
            catalog,
            ReceiptStore(namespace, SimThreads()),
            serial_replay_bindings(CaptureCommand, within_seconds=within),
            host=ReplayOperationHost("run", store, sim.clock),
        )
        runtime = ReplayRuntime(store, program, replay.ReplayAllowance(iterations=1))
        return ReplayRunHost(
            runtime,
            executor,
            sim.clock,
            lambda _: ReplayEnding(
                Ended(outcome=Outcome.FINISHED, summary="Done.", resumable=True)
            ),
        )

    return World(host, commands, workspaces, roadmap)


def launch(request: str) -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="run"), request, "host-a", 10)


@pytest.mark.parametrize("storage", ["fake", "document"])
@pytest.mark.parametrize("boundary", ["prepared", "executed", "observed", "accepted"])
@pytest.mark.parametrize("effect", [0, 1, 2])
async def test_real_serial_effects_recover_every_boundary_without_duplicate_capture(
    tmp_path: Path, sim: Sim, storage: str, boundary: str, effect: int
) -> None:
    given = world(tmp_path, sim, storage)
    first = given.host()
    first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    for ordinal in range(effect + 1):
        (span,) = first.runtime.prepare(now=sim.clock.now(), at=Timestamp())
        if ordinal == effect and boundary == "prepared":
            break
        result = await first.runtime.dispatch(span.id, first.executor, now=sim.clock.now())
        if ordinal == effect and boundary == "executed":
            break
        first.runtime.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
        if ordinal == effect and boundary == "observed":
            break
        first.runtime.accept(span.id, now=sim.clock.now(), at=Timestamp())
    first.runtime.close(now=sim.clock.now())
    result = await run_replay(given.host(), launch("resume"))
    assert isinstance(result, ReplayCompleted)
    assert isinstance(result.value, tuple)
    put, read, captured = result.value
    assert isinstance(put, pb.SerialRoadmapPut)
    assert isinstance(read, pb.SerialRoadmapRead)
    assert isinstance(captured, pb.SerialAttributionCaptured)
    assert put.status is pb.SerialEffectStatus.SUCCEEDED
    assert [item.id for item in read.items] == ["major-1"]
    assert captured.status is pb.SerialEffectStatus.SUCCEEDED
    assert captured.output == "profile result"
    assert captured.exit_code == 0
    assert len(given.commands.calls) == 1
    assert len(given.workspaces.candidates) == 1
    assert all(candidate.discarded for candidate in given.workspaces.candidates)
    assert await run_replay(given.host(), launch("again")) == result
    assert len(given.commands.calls) == 1
    assert [item.id for item in given.roadmap.items()] == ["major-1"]


@pytest.mark.parametrize("status", ["succeeded", "rejected", "failed"])
@pytest.mark.parametrize("exit_code", [None, 0, 2])
def test_native_codecs_preserve_failure_details_and_absent_exit_status(
    status: Literal["succeeded", "rejected", "failed"], exit_code: int | None
) -> None:
    read, put, capture = serial_replay_bindings(CaptureCommand, within_seconds=30)
    expected = {
        "succeeded": pb.SerialEffectStatus.SUCCEEDED,
        "rejected": pb.SerialEffectStatus.REJECTED,
        "failed": pb.SerialEffectStatus.FAILED,
    }[status]
    read_result = read.encode(RoadmapSnapshot(status=status, detail="read detail"))
    assert isinstance(read_result, pb.SerialRoadmapRead)
    assert read_result.status == expected
    assert read_result.detail == "read detail"
    put_result = put.encode(RoadmapItemsPut(status=status, detail="put detail"))
    assert isinstance(put_result, pb.SerialRoadmapPut)
    assert put_result.status == expected
    assert put_result.detail == "put detail"
    result = capture.encode(
        CommandCapture(
            status=status, exit_code=exit_code, output="output", truncated=True, detail="detail"
        )
    )
    assert isinstance(result, pb.SerialAttributionCaptured)
    assert result.status == expected
    assert result.has_field("exit_code") == (exit_code is not None)
    if exit_code is not None:
        assert result.exit_code == exit_code
    assert result.output == "output"
    assert result.truncated
    assert result.detail == "detail"


@pytest.mark.parametrize("timeout", [31, 60, 2**32 - 1])
def test_capture_rejects_a_timeout_larger_than_the_execution_owner_bound(timeout: int) -> None:
    binding = serial_replay_bindings(CaptureCommand, within_seconds=30)[2]
    revision = to_proto(RevisionRef.of_git_commit(COMMIT))
    assert isinstance(revision, core_leaves.RevisionRef)
    request = pb.CaptureSerialAttribution(
        revision=revision, argv=["profile"], output_argument="--output", timeout_seconds=timeout
    )
    # Historical inspection still decodes the exact original request.
    decoded = binding.decode(request)
    assert isinstance(decoded, CaptureCommand)
    assert decoded.timeout_seconds == timeout
    assert binding.dispatch_check is not None
    with pytest.raises(ValueError, match="exceeds the declared bound"):
        binding.dispatch_check(request)


async def test_recovered_capture_keeps_its_original_timeout_after_policy_tightens(
    tmp_path: Path, sim: Sim
) -> None:
    given = world(tmp_path, sim, "document")
    first = given.host()
    first.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    for ordinal in range(3):
        (span,) = first.runtime.prepare(now=sim.clock.now(), at=Timestamp())
        result = await first.runtime.dispatch(span.id, first.executor, now=sim.clock.now())
        if ordinal < 2:
            first.runtime.observe(span.id, result, now=sim.clock.now(), at=Timestamp())
            first.runtime.accept(span.id, now=sim.clock.now(), at=Timestamp())
    first.runtime.close(now=sim.clock.now())
    assert len(given.commands.calls) == 1
    assert isinstance(
        await run_replay(given.host(within_seconds=5), launch("resume")), ReplayCompleted
    )
    assert len(given.commands.calls) == 1
    assert given.commands.calls[0].timeout_seconds == 10


async def test_unused_paid_capture_holds_new_roadmap_writes_until_plain_resume(
    tmp_path: Path, sim: Sim
) -> None:
    given = world(tmp_path, sim, "document")
    assert isinstance(await run_replay(given.host(), launch("first")), ReplayCompleted)

    async def edited(ctx: ReplayFlow) -> object:
        await roadmap_calls(ctx)
        return await call(
            ctx,
            "roadmap.put_items",
            pb.PutSerialRoadmap(
                items=[
                    pb.SerialRoadmapItem(
                        id="major-2",
                        kind=pb.RoadmapKind.MAJOR,
                        title="New direction",
                        status=pb.RoadmapProgress.TODO,
                    )
                ]
            ),
            pb.SerialRoadmapPut,
            item="round-2",
        )

    assert await run_replay(given.host(edited), launch("notice")) == ReplayParked()
    assert [item.id for item in given.roadmap.items()] == ["major-1"]
    assert len(given.commands.calls) == 1
    assert isinstance(await run_replay(given.host(edited), launch("ack")), ReplayCompleted)
    assert [item.id for item in given.roadmap.items()] == ["major-1", "major-2"]
    assert len(given.commands.calls) == 1
