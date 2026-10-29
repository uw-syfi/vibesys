"""A logical turn start retains its deadline across a crash before child creation."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey, classify_context
from vs_project.api import Committed, FakeStateStore, StoredEnvelope
from vs_proto.api import ReplayStore, core_common, replay, replay_agent
from vs_runtime.api.replay import (
    REPLAY_STORE_SCHEMA_VERSION,
    FakeReplayExecutor,
    ReplayAbsent,
    ReplayResult,
    ReplayRuntime,
)

from .test_replay_run import launch
from .test_replay_sessions import SCOPE, SESSION


@pytest.mark.regression
@example(recovered_at=50, within=30)
@given(
    recovered_at=st.integers(min_value=1, max_value=1000),
    within=st.integers(min_value=1, max_value=1000),
)
async def test_turn_recovery_does_not_restart_the_recorded_deadline(
    recovered_at: int, within: int
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        artifact = core_common.ArtifactRef(
            artifact_id=core_common.ArtifactId(root="context", kind="artifact"),
            digest="sha256:input",
        )
        request = replay_agent.ReplayAgentRequest(
            scope=SCOPE,
            within_seconds=within,
            action=Oneof(
                "turn",
                replay_agent.ReplayAgentTurn(
                    session=SESSION,
                    invocation="first",
                    workspace=SCOPE,
                    prompts=[artifact],
                    artifact_dependencies=[artifact],
                    output_schema=core_common.SchemaRef(name="reply", version="1"),
                    max_turns=1,
                ),
            ),
        )
        return await ctx.call(
            ReplayCall(
                SemanticKey("agent.turn"),
                classify_context(request, contract="agent.v1"),
                "agent.v1",
                replay.ReplayPolicy(reply_type=replay_agent.ReplayAgentResult.desc().type_name),
                replay_agent.ReplayAgentResult,
            )
        )

    old = ReplayRuntime(FakeStateStore(), root, replay.ReplayAllowance())
    old.start(launch("first"), now=0, at=Timestamp())
    (child,) = old.prepare(now=0, at=Timestamp())
    assert child.replay_physical is not None
    original_deadline = child.replay_physical.deadline
    records = ReplayStore.from_binary(old.archive.encode()).records
    old.close(now=0)
    # Only the complete prefix before the physical start survives. No cache or
    # scheduling snapshot supplies the original bound to the new owner.
    store = FakeStateStore()
    fence = store.acquire("install", 0, 1)
    assert fence is not None
    installed = store.commit(
        None,
        StoredEnvelope(
            revision=0,
            schema_version=REPLAY_STORE_SCHEMA_VERSION,
            payload=ReplayStore(records=records[:-1]).to_binary(),
        ),
        fence,
        0,
    )
    assert isinstance(installed, Committed)
    assert store.release(fence, 0)
    fresh = ReplayRuntime(store, root, replay.ReplayAllowance())
    fresh.start(launch("resume"), now=recovered_at, at=Timestamp())
    owner = FakeReplayExecutor(lambda _: ReplayResult(core_common.Identity(root="unused")))
    try:
        for span in fresh.recovery:
            inspected = await fresh.inspect(span.id, owner, now=recovered_at)
            assert isinstance(inspected, ReplayAbsent)
            fresh.recover(span.id, inspected, now=recovered_at, at=Timestamp())
        fresh.finish_recovery()
        (replacement,) = fresh.prepare(now=recovered_at, at=Timestamp())
        assert replacement.replay_physical is not None
        assert replacement.replay_physical.deadline == original_deadline
        assert not owner.invocations
    finally:
        fresh.close(now=recovered_at)
