"""Release facts preserve their canonical source and physical ownership."""

import hashlib
import json
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.proofs import Mismatch, Missing, ProofField, ProofReason, Proven, released_owner

type OwnerKind = Literal["intent", "job", "builtin", "child", "session"]


def digest(value: core.RequestBase) -> str:
    return hashlib.sha256(
        json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@st.composite
def release_facts(
    draw: st.DrawFn, kind: OwnerKind, *, run_owned: bool = False
) -> tuple[
    core.Intent | core.OwnedJob | core.RegisteredOwnedJob | core.ChildLease | core.SessionView,
    tuple[core.Intent, ...],
]:
    identity = draw(st.integers(min_value=0, max_value=10000))
    scope = core.Scope(
        owner=core.RunId(root=f"run:{identity}")
        if run_owned
        else core.AttemptId(root=f"attempt:{identity}"),
        generation=identity,
    )
    episode = None if run_owned else core.DecisionId(root=f"episode:{identity}")
    resource = core.ResourceId(root=f"resource:{identity}")
    session = core.SessionSpec(
        session_id=core.SessionId(root=f"session:{identity}"),
        role_id=core.RoleId(root="role"),
        policy="fresh",
        lifetime="ephemeral",
        access=core.Access.READ_ONLY,
    )
    request: core.Request = core.DispatchTurn(
        request_id=core.RequestId(root=f"request:{identity}"),
        scope=scope,
        admission_id=episode,
        deadline_at=100.0,
        turn=core.TurnSpec(
            session=session,
            invocation_id=core.InvocationId(root=f"invocation:{identity}"),
            workspace=scope,
            prompts=(),
            output_schema=core.SchemaRef(name="output", version=1),
            deadline_at=100.0,
            charge_class="free",
        ),
    )
    if kind == "job":
        request = core.ExecuteRegisteredOperation(
            request_id=request.request_id,
            scope=scope,
            admission_id=episode,
            deadline_at=100.0,
            operation_id=core.OperationId(root=f"operation:{identity}"),
            operation=core.OperationWire(
                schema_ref=core.OperationSchemaRef(
                    kind="test.job",
                    request_schema=core.SchemaRef(name="job", version=1),
                    outcome_schema=core.SchemaRef(name="job-result", version=1),
                    lifecycle=core.LifecycleClass.OWNED_JOB,
                ),
                payload_json="{}",
            ),
            retry_limit=0,
        )
    elif kind == "builtin":
        request = core.SubmitMeasurement(
            request_id=request.request_id,
            scope=scope,
            admission_id=episode,
            deadline_at=100.0,
            plan=core.MeasurementPlan(
                purpose="official",
                candidate=core.initial_state().run.facts.baseline,
                evaluator_digest="evaluator",
                workload_digest="workload",
                environment_digest="environment",
                stages=(core.MeasurementStage(stage_id="stage", execution_budget=1.0),),
                policy="ordered",
                recipe=core.ArtifactRef(
                    artifact_id=core.ArtifactId(root="recipe"), digest="recipe"
                ),
                submitted_at=0.0,
                queue_allowance=0.0,
                deadline_at=1.0,
            ),
        )
    elif kind == "session":
        request = core.CloseSession(
            request_id=request.request_id,
            scope=scope,
            admission_id=episode,
            deadline_at=100.0,
            session_id=session.session_id,
        )
    assert request.request_id is not None
    observation = core.Observation(
        event_id=core.EventId(root=f"event:{identity}"),
        request_id=request.request_id,
        scope=scope,
        admission_id=episode,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        resource_id=resource,
        accepted=True,
        terminal=True,
        released=True,
        children_complete=True,
    )
    source = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=digest(request),
        lifecycle={
            "intent": core.LifecycleClass.SESSION_TURN,
            "job": core.LifecycleClass.OWNED_JOB,
            "builtin": core.LifecycleClass.OWNED_JOB,
            "child": core.LifecycleClass.SESSION_TURN,
            "session": core.LifecycleClass.IDEMPOTENT_WRITE,
        }[kind],
        phase=core.IntentPhase.COMPLETED,
        observation=observation,
        reconcile_deadline_at=100.0,
    )
    if kind == "job":
        assert isinstance(request, core.ExecuteRegisteredOperation)
        owner = core.RegisteredOwnedJob(
            operation_id=request.operation_id,
            request_id=request.request_id,
            scope=scope,
            resource_pool=core.PoolId(root="pool"),
            resource_id=resource,
            observation=observation,
            status=observation.status,
            terminal=True,
            released=True,
        )
    elif kind == "builtin":
        assert isinstance(request, core.SubmitMeasurement)
        owner = core.OwnedJob(
            submission_id=source.request_id,
            scope=scope,
            plan=request.plan,
            resource_id=resource,
            observation=observation,
            status=observation.status,
            terminal=True,
            released=True,
        )
    elif kind == "child":
        owner = core.ChildLease(
            resource_id=resource,
            scope=scope,
            source_requests=(source.request_id,),
            observation=observation,
            observation_watermarks=(
                core.ChildObservationWatermark(
                    source_request=source.request_id, observation=observation
                ),
            ),
            watermark_history_complete=True,
        )
    elif kind == "session":
        owner = core.SessionView(
            spec=session,
            scope=scope,
            generation=scope.generation,
            phase=core.SessionPhase.TERMINAL,
            resource_id=resource,
        )
    else:
        owner = source
    return owner, (source,)


@pytest.mark.parametrize("kind", ["intent", "job", "builtin", "child", "session"])
@given(data=st.data())
def test_exact_owner_release_and_missing_canonical_sources(
    kind: OwnerKind, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts(kind))
    proof = released_owner(owner, sources)
    assert isinstance(proof, Proven)
    assert proof.value == sources[0].observation
    if kind != "intent":
        assert released_owner(owner, ()) == Missing(ProofReason.ABSENT_REQUEST)
    assert released_owner(None, sources) == Missing(ProofReason.ABSENT_REQUEST)


@pytest.mark.parametrize("kind", ["job", "builtin", "child"])
@given(data=st.data(), run_owned=st.booleans(), bad_source=st.booleans())
def test_release_requires_the_unique_canonical_request_for_every_owner(
    kind: OwnerKind, data: st.DataObject, *, run_owned: bool, bad_source: bool
) -> None:
    """Run ownership does not waive the request payload, digest and lifecycle proof."""
    owner, sources = data.draw(release_facts(kind, run_owned=run_owned))
    assert isinstance(released_owner(owner, sources), Proven)
    assert released_owner(owner, ()) == Missing(ProofReason.ABSENT_REQUEST)
    if bad_source:
        forged = sources[0].model_copy(update={"payload_digest": "forged"})
        assert released_owner(owner, (forged,)) == Mismatch(ProofField.DIGEST)


@pytest.mark.parametrize("kind", ["intent", "job", "builtin"])
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("request_id", Mismatch(ProofField.REQUEST_ID)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("admission_id", Mismatch(ProofField.ADMISSION_ID)),
        ("terminal", Missing(ProofReason.UNRESOLVED)),
        ("released", Missing(ProofReason.UNRESOLVED)),
        ("children_complete", Missing(ProofReason.INCOMPLETE_MANIFEST)),
        ("resource_id", Missing(ProofReason.ABSENT_RESOURCE)),
    ],
)
@given(data=st.data())
def test_owner_release_rejects_each_corrupted_observation(
    kind: OwnerKind,
    field: str,
    expected: Missing | Mismatch,
    data: st.DataObject,
) -> None:
    owner, sources = data.draw(release_facts(kind))
    assert isinstance(owner, (core.Intent, core.OwnedJob, core.RegisteredOwnedJob))
    observation = owner.observation
    assert observation is not None
    changed = {
        "request_id": core.RequestId(root="foreign"),
        "scope": core.Scope(owner=core.AttemptId(root="foreign"), generation=0),
        "admission_id": core.DecisionId(root="foreign"),
        "terminal": False,
        "released": False,
        "children_complete": False,
        "resource_id": None,
    }[field]
    owner = owner.model_copy(
        update={"observation": observation.model_copy(update={field: changed})}
    )
    if kind in ("job", "builtin") and field == "resource_id":
        expected = Mismatch(ProofField.RESOURCE_ID)
    assert released_owner(owner, sources) == expected
    assert released_owner(owner.model_copy(update={"observation": None}), sources) == Missing(
        ProofReason.ABSENT_OBSERVATION
    )


@pytest.mark.parametrize("kind", ["intent", "job", "builtin", "child", "session"])
@pytest.mark.parametrize("missing", ["request", "observation"])
@given(data=st.data())
def test_release_requires_every_recorded_attempt_episode(
    kind: OwnerKind, missing: str, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts(kind))
    source = sources[0]
    assert source.observation is not None
    if missing == "request":
        request = source.request.model_copy(update={"admission_id": None})
        source = source.model_copy(update={"request": request, "payload_digest": digest(request)})
    else:
        observation = source.observation.model_copy(update={"admission_id": None})
        if kind == "session":
            source = source.model_copy(update={"observation": observation})
        elif kind == "child":
            owner = owner.model_copy(
                update={
                    "observation": observation,
                    "observation_watermarks": (
                        core.ChildObservationWatermark(
                            source_request=source.request_id, observation=observation
                        ),
                    ),
                }
            )
        else:
            owner = owner.model_copy(update={"observation": observation})
    if kind == "intent" and missing == "request":
        owner = source
    assert released_owner(owner, (source,)) == Missing(ProofReason.ABSENT_EPISODE)


@pytest.mark.parametrize("kind", ["job", "builtin"])
@given(data=st.data(), status=st.sampled_from(tuple(core.ObservationStatus)))
def test_job_status_is_an_independent_release_fact(
    kind: OwnerKind, data: st.DataObject, status: core.ObservationStatus
) -> None:
    owner, sources = data.draw(release_facts(kind))
    assert isinstance(owner, (core.OwnedJob, core.RegisteredOwnedJob))
    observation = owner.observation
    assert observation is not None
    owner = owner.model_copy(update={"status": status})
    expected = Proven(observation) if status == observation.status else Mismatch(ProofField.STATUS)
    assert released_owner(owner, sources) == expected


@pytest.mark.parametrize(
    "status",
    [
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    ],
)
@given(data=st.data())
def test_conclusive_negative_session_turn_can_release_without_a_resource(
    status: core.ObservationStatus, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts("intent"))
    assert isinstance(owner, core.Intent)
    assert owner.observation is not None
    observation = owner.observation.model_copy(
        update={"accepted": False, "status": status, "resource_id": None}
    )
    owner = owner.model_copy(update={"observation": observation})
    assert released_owner(owner, sources) == Proven(observation)


@pytest.mark.parametrize("kind", ["job", "builtin", "child", "session"])
@given(data=st.data())
def test_ambiguous_release_sources_deny_and_source_digest_is_independent(
    kind: OwnerKind, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts(kind))
    assert released_owner(owner, sources * 2) == Mismatch(ProofField.REQUEST_ID)
    source = sources[0].model_copy(update={"payload_digest": "wrong"})
    assert released_owner(owner, (source,)) == Mismatch(ProofField.DIGEST)


@pytest.mark.parametrize("retained", [False, True])
@given(data=st.data())
def test_child_release_requires_a_complete_nonempty_source_manifest(
    *, retained: bool, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts("child"))
    assert isinstance(owner, core.ChildLease)
    owner = owner.model_copy(
        update={
            "observation_watermarks": owner.observation_watermarks if retained else (),
            "watermark_history_complete": False,
        }
    )
    assert released_owner(owner, sources) == Missing(ProofReason.INCOMPLETE_HISTORY)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("resource_id", Missing(ProofReason.ABSENT_RESOURCE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("phase", Missing(ProofReason.UNRESOLVED)),
    ],
)
@given(data=st.data())
def test_session_release_requires_its_lease_generation_and_terminal_state(
    field: str, expected: Missing | Mismatch, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts("session"))
    assert isinstance(owner, core.SessionView)
    changed = {
        "resource_id": None,
        "generation": owner.generation + 1,
        "phase": core.SessionPhase.CLOSING,
    }[field]
    owner = owner.model_copy(update={field: changed})
    assert released_owner(owner, sources) == expected


@pytest.mark.parametrize("kind", ["intent", "job", "builtin", "child", "session"])
@given(data=st.data())
def test_release_requires_the_canonical_requests_optional_identity(
    kind: OwnerKind, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts(kind))
    source = sources[0]
    request = source.request.model_copy(update={"request_id": None})
    source = source.model_copy(update={"request": request, "payload_digest": digest(request)})
    if kind == "intent":
        owner = source
    assert released_owner(owner, (source,)) == Missing(ProofReason.ABSENT_REQUEST)


@pytest.mark.parametrize("status", tuple(core.ObservationStatus))
@given(data=st.data())
def test_accepted_session_turn_never_proves_release_without_resource(
    status: core.ObservationStatus, data: st.DataObject
) -> None:
    owner, sources = data.draw(release_facts("intent"))
    assert isinstance(owner, core.Intent)
    assert owner.observation is not None
    observation = owner.observation.model_copy(
        update={"accepted": True, "status": status, "resource_id": None}
    )
    owner = owner.model_copy(update={"observation": observation})
    assert released_owner(owner, sources) == Missing(ProofReason.ABSENT_RESOURCE)


@pytest.mark.parametrize(
    "status",
    [
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    ],
)
@given(data=st.data(), accepted=st.booleans())
def test_negative_close_command_cannot_certify_an_unidentified_session_lease(
    status: core.ObservationStatus, data: st.DataObject, *, accepted: bool
) -> None:
    owner, sources = data.draw(release_facts("session"))
    assert isinstance(owner, core.SessionView)
    source = sources[0]
    assert source.observation is not None
    observation = source.observation.model_copy(
        update={"accepted": False, "status": status, "resource_id": None}
    )
    source = source.model_copy(update={"observation": observation})
    owner = owner.model_copy(update={"accepted": accepted, "resource_id": None})
    assert released_owner(owner, (source,)) == Missing(ProofReason.ABSENT_RESOURCE)
