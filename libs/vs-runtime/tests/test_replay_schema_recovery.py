"""A compatible old reply stays usable when unrelated declarations in its file change."""

from collections.abc import Callable, Coroutine

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayError, ReplayFlow, SemanticKey, classify_context
from vs_project.api import Committed, FakeStateStore, StoredEnvelope
from vs_proto.api import ReplayStore, ValidationError, core_common, replay, validate
from vs_runtime.api.replay import FakeReplayExecutor, ReplayCompleted, ReplayResult, drive_replay
from vs_sim.api.testing import Sim

from .generated.prior.vibesys.runtime.testing.v1.schema_recovery_pb import (
    BoundedValue as PriorBoundedValue,
)
from .generated.prior.vibesys.runtime.testing.v1.schema_recovery_pb import Reply as PriorReply
from .generated.vibesys.runtime.testing.v1.schema_recovery_pb import (
    BoundedValue,
    RecoveryContext,
    Reply,
)
from .test_replay_run import host, launch


def program(
    reply: type[Reply] | type[PriorReply],
) -> Callable[[ReplayFlow], Coroutine[object, object, object]]:
    async def root(ctx: ReplayFlow) -> object:
        value = await ctx.call(
            ReplayCall(
                SemanticKey("work"),
                core_common.Identity(root="candidate"),
                "work.v1",
                replay.ReplayPolicy(reply_type=reply.desc().type_name),
                reply,
            )
        )
        return value.value

    return root


@pytest.mark.regression
@pytest.mark.parametrize("boundary", ["unobserved", "observed", "accepted"])
async def test_old_completed_reply_does_not_redeclare_its_whole_file_in_the_new_epoch(
    sim: Sim, boundary: str
) -> None:
    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda _: ReplayResult(PriorReply(value="answer"), paid=True))
    first = host(store, program(PriorReply), executor, sim)
    first.runtime.start(launch("first"), now=0, at=Timestamp())
    (span,) = first.runtime.prepare(now=0, at=Timestamp())
    outcome = await first.runtime.dispatch(span.id, executor, now=0)
    if boundary != "unobserved":
        first.runtime.observe(span.id, outcome, now=0, at=Timestamp())
    if boundary == "accepted":
        first.runtime.accept(span.id, now=0, at=Timestamp())
    first.runtime.close(now=0)
    resumed = host(store, program(Reply), executor, sim)
    assert await drive_replay(resumed, launch("resume")) == ReplayCompleted("answer")
    assert executor.invocations == (span.id,)
    assert resumed.runtime.archive.policy_drift is None
    first_schema = resumed.runtime.archive.epochs[0].descriptors
    current_schema = resumed.runtime.archive.current_epoch.descriptors
    assert first_schema is not None
    assert current_schema is not None
    assert PriorReply.desc().file.name == Reply.desc().file.name
    assert PriorReply.desc().file.proto != Reply.desc().file.proto
    assert PriorReply.desc().file.proto in first_schema.file
    assert Reply.desc().file.proto in current_schema.file
    classified = classify_context(RecoveryContext(candidate="candidate"), contract="work.v1")
    assert classified.message() == RecoveryContext(candidate="candidate")


@pytest.mark.regression
@given(bit=st.integers(min_value=0, max_value=255))
@example(bit=0)
def test_observation_checks_the_retained_reply_proof_before_writing(bit: int) -> None:
    sim = Sim(seed=0)
    original = FakeStateStore()
    owner = FakeReplayExecutor(lambda _: ReplayResult(PriorReply(value="answer")))
    first = host(original, program(PriorReply), owner, sim).runtime
    first.start(launch("first"), now=0, at=Timestamp())
    (span,) = first.prepare(now=0, at=Timestamp())
    first.close(now=0)
    stored = original.load()
    assert isinstance(stored, StoredEnvelope)
    records = ReplayStore.from_binary(stored.payload)
    for record in records.records:
        if record.body is not None and record.body.field == "span":
            saved = record.body.value.span
            assert saved is not None
            assert saved.replay is not None
            assert saved.replay.policy is not None
            proof = bytearray(saved.replay.policy.reply_schema_digest)
            proof[bit // 8] ^= 1 << (bit % 8)
            saved.replay.policy.reply_schema_digest = bytes(proof)
    store = FakeStateStore()
    fence = store.acquire("seed-host", now=0, duration=1)
    assert fence is not None
    assert isinstance(
        store.commit(
            None,
            StoredEnvelope(
                revision=0, schema_version=stored.schema_version, payload=records.to_binary()
            ),
            fence,
            now=0,
        ),
        Committed,
    )
    assert store.release(fence, now=0)
    resumed = host(store, program(PriorReply), owner, sim).runtime
    resumed.start(launch("resume"), now=0, at=Timestamp())
    before = resumed.archive.encode()
    with pytest.raises(ReplayError, match="reply contract"):
        resumed.recover(span.id, ReplayResult(PriorReply(value="answer")), now=0, at=Timestamp())
    assert resumed.archive.encode() == before
    assert owner.invocations == ()
    resumed.close(now=0)


@pytest.mark.regression
def test_retained_schemas_with_equal_names_keep_their_own_validation_rules() -> None:
    validate(PriorBoundedValue(value="x"))
    with pytest.raises(ValidationError):
        validate(BoundedValue(value="x"))
    validate(BoundedValue(value="xx"))
    with pytest.raises(ValidationError):
        validate(PriorBoundedValue(value=""))
