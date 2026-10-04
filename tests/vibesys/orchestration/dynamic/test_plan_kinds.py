"""A planned workstream is an implement or a profile workstream, chosen by its ``kind``.

The planner's reply is parsed at the plan boundary: each entry validates as
exactly its kind, an entry without a kind is an implement workstream (as plans
written before profile workstreams were), and an unknown key or kind is
rejected at the entry's location for the planner's correction turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import agentshim
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.evaluation_scenarios import (
    Producer,
    ScenarioOutcome,
    ScenarioSpec,
    build_scenario,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.models import (
    DynamicProfile,
    DynamicState,
    PortfolioPlan,
    ProfileDecision,
    ProfilePlan,
    WorkstreamKind,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.profiles import unavailable_profile_fields
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
    SemanticEvaluationStage,
)
from vs_evaluation.api import (
    AvailabilitySnapshot,
    ContentDigest,
    EvaluationAgentService,
    EvaluationRequest,
    EvidenceKind,
    ProfilerAgentResult,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ResourceRequirements,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor, FakeProfilerTurnProvision
from vs_project.api import StateNamespace
from vs_runtime.api import CandidateProfile, CandidateProfileStatus, ProfileField
from vs_runtime.api.testing import FakeRun
from vs_sandbox.api.slurm import profile_capture_descriptor

# Canonical IDs only: test_plan_ids covers the spelling rules.
_IDS = st.text(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.", min_size=1, max_size=24
)
_KINDS = st.sampled_from(["implement", "implicit", "profile"])
_TARGETS = st.one_of(st.none(), _IDS)


def _entry(kind: str, identifier: str, target: str | None) -> dict[str, object]:
    if kind == "profile":
        return {
            "kind": "profile",
            "profile_id": identifier,
            "target_hypothesis_id": target,
            "question": "Where does the time go?",
            "decision_impact": "Prioritize the implementation that removes the dominant cost.",
        }
    entry: dict[str, object] = {
        "hypothesis_id": identifier,
        "title": "Investigate",
        "hypothesis": "A mechanism limits throughput.",
        "task": "Implement and verify it.",
        "pass_criteria": "Throughput improves.",
    }
    # ``implicit`` omits the kind, as a plan written before profiles did.
    return {"kind": "implement", **entry} if kind == "implement" else entry


@st.composite
def _plans(draw: st.DrawFn) -> tuple[list[str], dict[str, object]]:
    identifiers = draw(st.lists(_IDS, min_size=1, max_size=6, unique=True))
    kinds = [draw(_KINDS) for _ in identifiers]
    entries = [
        _entry(kind, identifier, draw(_TARGETS))
        for kind, identifier in zip(kinds, identifiers, strict=True)
    ]
    return kinds, {"reasoning": "r", "workstreams": entries, "hypothesis_updates": []}


@given(_plans())
def test_each_entry_validates_as_its_kind_and_round_trips(
    case: tuple[list[str], dict[str, object]],
) -> None:
    kinds, data = case

    plan = PortfolioPlan.model_validate(data)

    expected = [ProfileDecision if kind == "profile" else WorkstreamPlan for kind in kinds]
    assert [type(item) for item in plan.workstreams] == expected
    entries = data["workstreams"]
    assert isinstance(entries, list)
    assert [planned_id(item) for item in plan.workstreams] == [
        entry.get("profile_id", entry.get("hypothesis_id")) for entry in entries
    ]
    assert PortfolioPlan.model_validate_json(plan.model_dump_json(), strict=True) == plan


@given(_plans(), st.data())
def test_an_unknown_key_is_rejected_at_its_entry(
    case: tuple[list[str], dict[str, object]], data: st.DataObject
) -> None:
    kinds, plan = case
    entries = plan["workstreams"]
    assert isinstance(entries, list)
    position = data.draw(st.integers(0, len(entries) - 1))
    entries[position] = {**entries[position], "request_evaluation": True}

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    kind = "profile" if kinds[position] == "profile" else "implement"
    assert f"workstreams.{position}.{kind}.request_evaluation" in str(rejected.value)


@given(_plans(), st.text(min_size=1).filter(lambda value: value not in {"implement", "profile"}))
def test_an_unknown_kind_is_rejected_naming_the_kinds(
    case: tuple[list[str], dict[str, object]], kind: str
) -> None:
    _, plan = case
    entries = plan["workstreams"]
    assert isinstance(entries, list)
    entries[0] = {**entries[0], "kind": kind}

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert rejected.value.errors()[0]["loc"] == ("workstreams", 0)
    assert rejected.value.errors()[0]["type"] == "union_tag_invalid"


def test_the_reply_schema_is_in_the_strict_provider_subset() -> None:
    """Codex's strict structured output rejects ``oneOf``; the union is ``anyOf``."""
    schema = agentshim.normalize(PortfolioPlan.model_json_schema(), agentshim.SchemaDialect.STRICT)

    assert agentshim.dialect_problems(schema, agentshim.SchemaDialect.STRICT) == []


@given(
    st.sampled_from(CandidateProfileStatus),
    st.lists(_IDS, min_size=1, max_size=4, unique=True),
)
def test_a_state_with_profiles_loads_as_the_store_loads_it(
    status: CandidateProfileStatus, identifiers: list[str]
) -> None:
    failed = status is CandidateProfileStatus.FAILED
    profiles = [
        DynamicProfile(
            profile_id=identifier,
            sequence=sequence,
            planning_call=1,
            plan=ProfilePlan(
                kind=WorkstreamKind.PROFILE,
                profile_id=identifier,
                target_hypothesis_id=None,
                question="Where does the time go?",
            ),
            revision="rev",
            outcome=CandidateProfile(
                revision="rev",
                status=status,
                operation_id="op",
                diagnosis=None if failed else "Decode dominates.",
                failure="the profiler turn failed" if failed else None,
            ),
        )
        for sequence, identifier in enumerate(identifiers, start=1)
    ]
    state = DynamicState(profiles=profiles)

    # Persisted profiles predating decision_impact have no such key.
    legacy = state.model_dump(mode="json")
    for profile in legacy["profiles"]:
        profile["plan"].pop("decision_impact")
    loaded = DynamicState.model_validate_json(json.dumps(legacy), strict=True)

    assert loaded == state
    assert loaded.scheduled() == len(identifiers)


@given(
    requested=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple),
    unavailable=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple),
)
def test_only_known_missing_fields_are_removed_from_future_requests(
    requested: tuple[ProfileField, ...], unavailable: tuple[ProfileField, ...]
) -> None:
    prior = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="prior",
        target_hypothesis_id=None,
        question="Measure",
    )
    state = DynamicState(
        profiles=[
            DynamicProfile(
                profile_id="prior",
                sequence=1,
                planning_call=1,
                plan=prior,
                revision="old-revision",
                outcome=CandidateProfile(
                    revision="old-revision",
                    status=CandidateProfileStatus.UNSUPPORTED,
                    capture_started=False,
                    missing_fields=unavailable,
                    diagnosis="Unavailable",
                ),
            )
        ]
    )
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="next",
        target_hypothesis_id=None,
        question="Measure",
        required_fields=requested,
    )
    assert set(unavailable_profile_fields(state, plan)) == set(requested) & set(unavailable)
    assert state.unsupported_profiles() == 1
    assert state.unsupported_profiles(scope="capability") == (0 if unavailable else 1)


def test_an_unsupported_profile_that_examined_capture_evidence_keeps_its_charge() -> None:
    """r23: an inconclusive 176-stage-second capture incorrectly refunded a start."""
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="captured",
        target_hypothesis_id=None,
        question="Attribute phase costs",
    )
    state = DynamicState(
        profiles=[
            DynamicProfile(
                profile_id=plan.profile_id,
                sequence=1,
                planning_call=1,
                plan=plan,
                revision="rev",
                outcome=CandidateProfile(
                    revision="rev",
                    status=CandidateProfileStatus.UNSUPPORTED,
                    evidence_ids=("a" * 64,),
                    diagnosis="The completed capture lacks phase markers.",
                ),
            )
        ]
    )
    assert state.unsupported_profiles(scope="budget") == 0
    legacy = state.model_dump(mode="json")
    legacy["profiles"][0]["outcome"].pop("capture_started")
    assert DynamicState.model_validate(legacy).unsupported_profiles(scope="budget") == 0


@given(
    status=st.sampled_from(CandidateProfileStatus),
    capture_started=st.one_of(st.none(), st.booleans()),
    missing_fields=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple),
)
def test_refund_and_capability_policy_depend_on_distinct_trusted_facts(
    *,
    status: CandidateProfileStatus,
    capture_started: bool | None,
    missing_fields: tuple[ProfileField, ...],
) -> None:
    unsupported = status is CandidateProfileStatus.UNSUPPORTED
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="profile",
        target_hypothesis_id=None,
        question="Measure costs",
    )
    outcome = CandidateProfile(
        revision="rev",
        status=status,
        capture_started=capture_started,
        missing_fields=missing_fields if unsupported else (),
        failure="capture failed" if status is CandidateProfileStatus.FAILED else None,
    )
    profile = DynamicProfile(
        profile_id=plan.profile_id,
        sequence=1,
        planning_call=1,
        plan=plan,
        revision="rev",
        outcome=outcome,
    )
    state = DynamicState(profiles=[profile])
    assert profile.refundable == (unsupported and capture_started is False)
    assert state.unsupported_profiles(scope="budget") == profile.refundable
    assert state.unsupported_profiles(scope="capability") == (unsupported and not missing_fields)
    assert DynamicState.model_validate_json(state.model_dump_json(), strict=True) == state


@dataclass
class _ProfileExecutor(FakeEvaluationExecutor):
    """Replay real profile producer records through the executor's public interface."""

    root: Path = Path("/unused")
    command: tuple[str, ...] = ()
    outcome: ScenarioOutcome = ScenarioOutcome.CORRECTNESS_FAIL

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        snapshot = await super().availability(requirements)
        return snapshot.model_copy(
            update={
                "supported_profile_fields": profile_capture_descriptor(
                    self.command
                ).supported_fields
            }
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        await super().submit(request, handle_id=handle_id)
        capture = SemanticEvaluationStage.model_validate(request.stages[0].payload)
        async with build_scenario(
            self.root / handle_id,
            ScenarioSpec(
                revision=capture.snapshot,
                scope_id=request.owner_scope,
                kinds=(EvidenceKind.PROFILE,),
                outcome=self.outcome,
                failure="temporary bind failure",
            ),
            Producer.SLURM,
        ) as scenario:
            self.set_state(
                handle_id,
                scenario.record.state,
                stage_results=scenario.record.stage_results,
                failure=scenario.record.failure,
            )

    async def close(self) -> None:
        """The in-memory executor owns no external resources."""


class _ImmediateProfiler(FakeProfilerTurnProvision):
    """Answer a request that the profiler cannot interpret without declaring missing fields."""

    async def run_turn(
        self,
        *,
        session_id: str,
        operation_id: str,
        request: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
    ) -> ProfilerAgentResult:
        self.unsupported(operation_id, "this request cannot be interpreted")
        return await super().run_turn(
            session_id=session_id,
            operation_id=operation_id,
            request=request,
            scope_id=scope_id,
            candidate_snapshot_id=candidate_snapshot_id,
        )


def _namespace(root: Path, name: str) -> StateNamespace:
    path = root / ".vibesys" / "state" / name
    path.mkdir(parents=True)
    return StateNamespace(project_root=root, root=path, portable=False)


async def _snapshot(scope: str | None) -> str:
    return f"snapshot:{scope}"


async def _no_evidence(
    _principal: str, _scope: str | None, _snapshot_id: str, _ids: tuple[str, ...]
) -> tuple[TrustedEvidence, ...]:
    return ()


@pytest.mark.asyncio
@settings(max_examples=24)
@given(
    kind=st.sampled_from(("timeline", "counters")),
    outcome=st.sampled_from(tuple(ScenarioOutcome)),
    required=st.lists(st.sampled_from(ProfileField), min_size=1, unique=True).map(tuple),
)
@example(
    kind="timeline",
    outcome=ScenarioOutcome.CORRECTNESS_FAIL,
    required=(ProfileField.HIP_API_TIMING,),
)
@example(
    kind="timeline",
    outcome=ScenarioOutcome.INFRA_FAIL,
    required=(ProfileField.HIP_API_TIMING,),
)
@example(
    kind="timeline",
    outcome=ScenarioOutcome.TIMEOUT,
    required=(ProfileField.HIP_API_TIMING,),
)
@example(
    kind="timeline",
    outcome=ScenarioOutcome.PASS,
    required=(ProfileField.HIP_API_TIMING,),
)
async def test_descriptor_support_cross_capture_outcome_never_blacklists_supported_fields(
    tmp_path_factory: pytest.TempPathFactory,
    kind: str,
    outcome: ScenarioOutcome,
    required: tuple[ProfileField, ...],
) -> None:
    root = tmp_path_factory.mktemp("profile-outcome")
    command = (
        "python3",
        "remote_capture.py",
        "--request-json",
        json.dumps({"kind": kind, "options": {}}),
    )
    descriptor = profile_capture_descriptor(command)
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    revision = await run.workspaces.root.snapshot("failed-revision")
    executor = _ProfileExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.PROFILE.value,),
        root=root / "producer",
        command=command,
        outcome=outcome,
    )
    digest = ContentDigest.sha256(b"profile identity")
    backend = SemanticEvaluationBackend(
        run.evaluation,
        run.workspaces,
        _namespace(root, "evaluation"),
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
        executor=executor,
        submitted_time=executor.clock.monotonic,
    )
    provision = _ImmediateProfiler()
    profiler = ProfilerAgentService(
        provision,
        _namespace(root, "profiler"),
        ProfilerAgentServiceHooks(candidate_snapshot=_snapshot, resolve_evidence=_no_evidence),
    )
    evaluation = EvidenceReusingEvaluation(
        run.evaluation,
        backend,
        run_id=run.run_id,
        scopes=EvaluationAgentService(
            backend, _namespace(root, "access"), root / "evaluation.sock"
        ),
        profiler=profiler,
    )
    try:
        result = await evaluation.profile(
            revision, "HIP API timing", member_id="bad", required_fields=required
        )
        missing = tuple(field for field in required if field not in descriptor.supported_fields)
        assert result.missing_fields == missing
        if missing:
            assert result.status is CandidateProfileStatus.UNSUPPORTED
            assert not executor.submissions
        elif outcome is not ScenarioOutcome.PASS:
            assert result.status is CandidateProfileStatus.FAILED
            assert result.failure
            assert not provision.turns
        plan = ProfilePlan(
            kind=WorkstreamKind.PROFILE,
            profile_id="bad",
            target_hypothesis_id="bad-revision",
            question="HIP API timing",
            required_fields=required,
        )
        state = DynamicState(
            profiles=[
                DynamicProfile(
                    profile_id="bad",
                    sequence=1,
                    planning_call=1,
                    plan=plan,
                    revision=revision,
                    outcome=result,
                )
            ]
        )
        repaired = plan.model_copy(
            update={"profile_id": "fixed", "target_hypothesis_id": "fixed-revision"}
        )
        assert not set(unavailable_profile_fields(state, repaired)) & set(
            descriptor.supported_fields
        )
    finally:
        await profiler.close()
        await backend.close()
        await run.close()
