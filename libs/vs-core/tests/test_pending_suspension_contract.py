"""Pending suspension identity and absence through the public persisted contract."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core


def _invocation(generation: int, owner: core.RunId | core.AttemptId) -> core.Invocation:
    ref = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="yield"),
        generation=generation,
    )
    scope = core.Scope(owner=owner, generation=generation)
    return core.Invocation(
        invocation=ref,
        scope=scope,
        turn=core.TurnSpec(
            session=core.SessionSpec(
                session_id=ref.session_id,
                role_id=core.RoleId(root="worker"),
                policy="reuse",
                lifetime="owner",
                access=core.Access.WRITE_CANDIDATE,
            ),
            invocation_id=ref.invocation_id,
            workspace=scope,
            prompts=(),
            output_schema=core.SchemaRef(name="reply", version=1),
            deadline_at=100.0,
            charge_class="free",
        ),
        phase=core.SessionPhase.SUSPENDED,
    )


def _pending(invocation: core.Invocation, jobs: list[str], deadline: float) -> core.Continuation:
    return core.Continuation(
        continuation_id=core.ContinuationId(root="pending"),
        invocation=invocation.invocation,
        next_invocation=invocation.invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="resume")}
        ),
        jobs=tuple(core.ResourceId(root=job) for job in jobs),
        deadline_at=deadline,
        phase=core.ContinuationPhase.WAITING,
    )


def _envelope(invocation: core.Invocation) -> core.RunEnvelope[core.StrategyState]:
    state = core.initial_state()
    state = state.model_copy(update={"sessions": core.SessionsState(invocations=(invocation,))})
    return core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )


@given(
    generation=st.integers(min_value=0, max_value=1000),
    attempt_owned=st.booleans(),
    jobs=st.lists(st.text(alphabet="abc012", min_size=1, max_size=12), unique=True, max_size=8),
    deadline=st.floats(min_value=0.0, max_value=10000.0, allow_nan=False),
    phase=st.sampled_from(tuple(core.SessionPhase)),
)
def test_pending_suspension_survives_reload_and_unrelated_step_without_publication(
    *,
    generation: int,
    attempt_owned: bool,
    jobs: list[str],
    deadline: float,
    phase: core.SessionPhase,
) -> None:
    owner = core.AttemptId(root="attempt") if attempt_owned else core.RunId(root="run")
    invocation = _invocation(generation, owner)
    pending = _pending(invocation, jobs, deadline)
    invocation = core.Invocation.model_validate(
        {**invocation.model_dump(), "pending_suspension": pending, "phase": phase}
    )
    envelope = _envelope(invocation)
    codec = core.OperationRegistry()
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope))
    assert restored.core.sessions.invocations == (invocation,)
    assert restored.core.sessions.run_checkpoints == ()
    assert restored.core.evaluation.continuations == ()
    event = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
        now_at=1.0,
    )
    result = core.step(restored.core, event)
    assert result.state.sessions.invocations[0].pending_suspension == pending
    assert result.state.evaluation.continuations == ()
    assert result.requests == ()
    cleared = core.Invocation.model_validate(
        {**invocation.model_dump(), "pending_suspension": None}
    )
    consumed = _envelope(cleared)
    consumed = codec.decode_envelope(type(consumed), codec.encode_envelope(consumed))
    assert core.step(consumed.core, event).state.sessions.invocations[0].pending_suspension is None
    assert consumed.core.sessions.invocations[0].model_dump(exclude={"pending_suspension"}) == (
        invocation.model_dump(exclude={"pending_suspension"})
    )
    absent = consumed.model_dump(mode="json")
    absent["core"]["sessions"]["invocations"][0].pop("pending_suspension")
    restored_absence = codec.decode_envelope(type(consumed), json.dumps(absent))
    assert restored_absence.core.sessions.invocations[0].pending_suspension is None


@pytest.mark.parametrize(
    "mismatch",
    [
        "invocation",
        "scope",
        "turn_session",
        "turn_invocation",
        "successor_session",
        "successor_generation",
        "successor_reuse",
    ],
)
@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=1, max_value=1000))
def test_pending_suspension_rejects_mismatched_correspondence_in_public_codec(
    mismatch: str, generation: int, difference: int
) -> None:
    invocation = _invocation(generation, core.AttemptId(root="attempt"))
    payload = invocation.model_dump(mode="json")
    payload["pending_suspension"] = _pending(invocation, ["job"], 10.0).model_dump(mode="json")
    pending = payload["pending_suspension"]
    if mismatch == "invocation":
        pending["invocation"]["invocation_id"]["root"] = "foreign"
    elif mismatch == "scope":
        payload["scope"]["generation"] += difference
    elif mismatch == "turn_session":
        payload["turn"]["session"]["session_id"]["root"] = "foreign"
    elif mismatch == "turn_invocation":
        payload["turn"]["invocation_id"]["root"] = "foreign"
    elif mismatch == "successor_session":
        pending["next_invocation"]["session_id"]["root"] = "foreign"
    elif mismatch == "successor_generation":
        pending["next_invocation"]["generation"] += difference
    else:
        pending["next_invocation"] = pending["invocation"]
    envelope = _envelope(invocation).model_dump(mode="json")
    envelope["core"]["sessions"]["invocations"] = [payload]
    with pytest.raises(ValidationError, match="pending_suspension"):
        core.OperationRegistry().decode_envelope(
            core.RunEnvelope[core.StrategyState], json.dumps(envelope)
        )


@given(st.integers(min_value=0, max_value=1000))
def test_version2_migration_preserves_pending_absence_and_rejects_injected_field(
    generation: int,
) -> None:
    invocation = _invocation(generation, core.AttemptId(root="attempt"))
    legacy = invocation.model_dump(mode="json")
    legacy.pop("evaluation_prefix")
    legacy.pop("pending_suspension", None)
    old = json.loads((Path(__file__).parent / "fixtures" / "envelope-v2-main.json").read_text())
    old["core"]["sessions"]["invocations"] = [legacy]
    codec = core.OperationRegistry()
    loaded = codec.migrate_envelope(
        core.RunEnvelope[core.StrategyState], json.dumps(old), core.v2_to_v3_migration(codec)
    )
    assert loaded.core.sessions.invocations[0].pending_suspension is None
    assert loaded.core.sessions.invocations[0].invocation == invocation.invocation
    for injected in (None, _pending(invocation, ["job"], 10.0).model_dump(mode="json")):
        legacy["pending_suspension"] = injected
        with pytest.raises(core.ContractError, match="pending_suspension"):
            codec.migrate_envelope(
                core.RunEnvelope[core.StrategyState],
                json.dumps(old),
                core.v2_to_v3_migration(codec),
            )
