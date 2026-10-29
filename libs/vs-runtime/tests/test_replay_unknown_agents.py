"""Unknown agent ownership is resolved before a fresh physical conversation starts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_project.api import Project
from vs_proto.api import EpochStarted, core_common
from vs_proto.api import replay_agent as pb
from vs_runtime.api.replay import (
    ReplayCompleted,
    ReplayExecutors,
    ReplayLaunch,
    ReplayOperationError,
    run_replay,
)
from vs_runtime.api.testing import FakeWorkspace

from .generated.vibesys.runtime.testing.v1.agent_replay_pb import AgentReplayResult
from .test_replay_physical import proof_barrier
from .test_replay_run import host
from .test_replay_sessions import SCOPE, SESSION, step, world

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api.replay import ReplayFlow
    from vs_sim.api.testing import Sim


@pytest.mark.parametrize("implementation", ["fake", "client"])
def test_proven_unknown_agent_restarts_as_fresh_conversation(
    tmp_path: Path, sim: Sim, implementation: str
) -> None:
    given = world(tmp_path, sim, implementation)
    given.client.fail("worker", ConnectionError("provider lost acceptance"), times=1)

    async def root(ctx: ReplayFlow) -> object:
        ensured = await step(
            ctx, Oneof("ensure", pb.ReplayEnsureAgent(session=SESSION)), pb.ReplayAgentResult
        )
        assert ensured.session is not None
        session = deepcopy(SESSION)
        session.id = ensured.session.root
        session.acquisition = pb.ReplaySessionAcquisition.REUSE
        result = await step(
            ctx,
            Oneof(
                "turn",
                pb.ReplayAgentTurn(
                    session=session,
                    invocation="work",
                    workspace=SCOPE,
                    prompts=[
                        core_common.ArtifactRef(
                            artifact_id=core_common.ArtifactId(root="prompt", kind="artifact"),
                            digest="sha256:0",
                        )
                    ],
                    artifact_dependencies=[
                        core_common.ArtifactRef(
                            artifact_id=core_common.ArtifactId(root="context", kind="artifact"),
                            digest="sha256:context",
                        )
                    ],
                    output_schema=core_common.SchemaRef(name="reply", version="1"),
                    max_turns=1,
                ),
            ),
            AgentReplayResult,
        )
        return result.value

    async def scenario() -> None:
        initial = host(given.store, root, given.executor(None), sim)
        launch = ReplayLaunch(EpochStarted(run_id="run"), "first", "host-a", 100)
        initial.runtime.start(launch, now=sim.clock.now(), at=Timestamp())
        try:
            (ensure,) = initial.runtime.prepare(now=sim.clock.now(), at=Timestamp())
            ensured = await initial.runtime.dispatch(
                ensure.id, initial.executor, now=sim.clock.now()
            )
            initial.runtime.observe(ensure.id, ensured, now=sim.clock.now(), at=Timestamp())
            initial.runtime.accept(ensure.id, now=sim.clock.now(), at=Timestamp())
            (old,) = initial.runtime.prepare(now=sim.clock.now(), at=Timestamp())
            barrier = proof_barrier(Project.open(tmp_path), old, run_id="run")
            with pytest.raises(ReplayOperationError, match="remains unknown"):
                await initial.runtime.dispatch(old.id, initial.executor, now=sim.clock.now())
        finally:
            initial.runtime.close(now=sim.clock.now())
        workspace = given.resolver.guarded
        assert isinstance(workspace, FakeWorkspace)
        retained_input = workspace.revision
        resumed = replace(
            host(
                given.store,
                root,
                ReplayExecutors({pb.ReplayAgentRequest: given.executor(None)}),
                sim,
            ),
            writer_barrier=barrier,
        )
        launch = ReplayLaunch(EpochStarted(run_id="run"), "resume", "host-b", 100)
        assert await run_replay(resumed, launch) == ReplayCompleted(7)
        calls = given.client.calls_for("worker")
        assert len(calls) == 2
        assert calls[0].session_key != calls[1].session_key
        assert calls[0].invocation_id != calls[1].invocation_id
        children = resumed.runtime.archive.children(old.parent)
        assert len(children) == 2
        assert children[0].replay_physical is not None
        assert children[0].replay_physical.has_field("withdrawn_sequence")
        assert children[0].replay_physical.has_field("termination_proof_digest")
        # The pre-dispatch access receipt supplies the retained input, even after a host crash.
        assert workspace.agent_restore_calls[-1] == (retained_input, ())
        assert len(resumed.runtime.archive.history) == 2

    sim.run(scenario(), threads=given.threads)
