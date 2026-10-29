"""Pre-cutover APIs must refuse reuse after host no-op or released-hold recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api import AttemptId, AttemptRef, RevisionRef
from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey, classify_context
from vs_project.api import FakeStateStore
from vs_proto.api import EpochStarted, core_leaves, replay, replay_workspace
from vs_runtime.api.core import StoreWorkspaceReceipts
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.infrastructure import HostEnvironment
from vs_runtime.api.replay import (
    ReplayLaunch,
    ReplayOperationHost,
    ReplayRuntime,
    WorkspaceReplayExecutor,
)
from vs_runtime.api.testing import idle_environment_system

from .test_workspace_requests import _env, _executor

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api.testing import Sim


@pytest.mark.regression
@pytest.mark.parametrize("released_hold", [False, True])
def test_host_noop_and_released_holds_cannot_authorize_root_reuse(
    tmp_path: Path, sim: Sim, *, released_hold: bool
) -> None:
    with _env(tmp_path) as env:
        resources = env.hosts[0]
        revision = resources.root.revision
        assert revision is not None
        base = to_proto(RevisionRef.of_git_commit(revision))
        assert isinstance(base, core_leaves.RevisionRef)
        owner, receipts = _executor(resources)
        if released_hold:
            tables = StoreWorkspaceReceipts(receipts)
            old = AttemptRef(attempt_id=AttemptId(root="previous"), generation=0)
            tables.acquire_root(old)
            tables.release_root(old)
        HostEnvironment().reap_orphans(
            env.run_id, system=idle_environment_system(), log=lambda _: None
        )
        request = replay_workspace.ReplayWorkspaceRequest(
            attempt="new",
            admission="admit-new",
            action=Oneof(
                "open",
                replay_workspace.ReplayOpenWorkspace(
                    mode=replay_workspace.ReplayWorkspaceMode.EXCLUSIVE_ROOT, base=base
                ),
            ),
        )

        async def root(ctx: ReplayFlow) -> object:
            return await ctx.call(
                ReplayCall(
                    SemanticKey("workspace.open"),
                    classify_context(request, contract="workspace.v1"),
                    "workspace.v1",
                    replay.ReplayPolicy(
                        reply_type=replay_workspace.ReplayWorkspaceResult.desc().type_name
                    ),
                    replay_workspace.ReplayWorkspaceResult,
                )
            )

        async def scenario() -> None:
            store = FakeStateStore()
            runtime = ReplayRuntime(store, root, replay.ReplayAllowance())
            executor = WorkspaceReplayExecutor(
                owner,
                receipts,
                ReplayOperationHost(env.run_id, store, sim.clock),
                within_seconds=100,
            )
            runtime.start(
                ReplayLaunch(EpochStarted(run_id=env.run_id), "launch", "host-a", 100),
                now=sim.clock.now(),
                at=Timestamp(),
            )
            try:
                (span,) = runtime.prepare(now=sim.clock.now(), at=Timestamp())
                with pytest.raises(ValueError, match="Recovery blocked"):
                    await runtime.dispatch(span.id, executor, now=sim.clock.now())
            finally:
                runtime.close(now=sim.clock.now())

        sim.run(scenario())
