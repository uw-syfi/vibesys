"""Evidence ledger, InterpretEvidence and RetainVerifiedRevision owners, on public APIs only."""

import hashlib
import tempfile
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.support.runtime_evaluation import SCOPE, ScenarioCluster, build_stack, submission

from vs_core.api import (
    Capabilities,
    ContractError,
    EventId,
    EvidenceAcceptanceReceipt,
    EvidenceId,
    EvidenceKey,
    EvidenceKind,
    EvidenceRef,
    HostFence,
    HostId,
    InspectOwnedJob,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationDescriptor,
    OperationRegistration,
    OperationRequest,
    OperationSchemaRef,
    RequestId,
    ResourceId,
    RevisionId,
    RevisionRef,
    SchemaRef,
    StrategyDeclaration,
    StrategyId,
    Value,
    validate_startup,
)
from vs_evaluation.api import (
    ContentDigest,
    EvidenceFingerprints,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_project.api import Project
from vs_runtime.api.core import (
    Applied,
    ExecutionContext,
    InterpretEvidenceOwner,
    MeasurementRequests,
    NotApplied,
    OperationPorts,
    OperationRole,
    ReceiptEvidenceLedger,
    ReceiptStore,
    RetainRevisionOwner,
    build_operation_catalog,
    commit_of,
    production_owners,
    revision_ref,
)
from vs_runtime.api.testing import FakeEvidenceLedger, FakeWorkspace, FakeWorkspaces

pytestmark = pytest.mark.asyncio

DIGEST = "a" * 64


def _key(source: str, evidence_id: EvidenceId) -> EvidenceKey:
    return EvidenceKey(source_request=RequestId(root=source), evidence_id=evidence_id)


def _store(root: Path) -> ReceiptStore:
    (root / "project").mkdir(exist_ok=True)
    return ReceiptStore(Project.open(root / "project").state.state_store_namespace("run"))


def _evidence(
    seed: int, *, outcome: EvidenceOutcome = EvidenceOutcome.PASSED, value: float = 1.0
) -> TrustedEvidence:
    digest = ContentDigest(value=DIGEST)
    return TrustedEvidence(
        evidence_id=hashlib.sha256(str(seed).encode()).hexdigest(),
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=StageKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        trusted_inputs=digest,
        outcome=outcome,
        metrics=(EvidenceMetric(name="throughput", value=value, direction="max"),),
        accepted_round=0,
    )


def _ref(  # noqa: PLR0913  # lint-waiver: LW-940010 [PLR0913]; each keyword is an independent fact of one evidence reference.
    source: str,
    evidence: TrustedEvidence,
    *,
    kind: EvidenceKind = EvidenceKind.BENCHMARK,
    status: ObservationStatus = ObservationStatus.SUCCEEDED,
    candidate: str = "a" * 40,
    purpose: Literal["baseline", "local-validation", "official", "profile"] = "official",
    accepted: bool = True,
) -> EvidenceRef:
    receipt = EvidenceAcceptanceReceipt(
        observation=Observation(
            event_id=EventId(root="accepted"),
            request_id=RequestId(root=source),
            scope=SCOPE,
            sequence=0,
            observed_at=0.0,
            status=status,
            accepted=True,
            terminal=True,
        )
    )
    return EvidenceRef(
        acceptance_receipt=receipt if accepted else None,
        evidence_id=EvidenceId(root=evidence.evidence_id),
        kind=kind,
        purpose=purpose,
        scope=SCOPE,
        source_request=RequestId(root=source),
        candidate=revision_ref(candidate),
        observation_sequence=0,
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        provenance="trusted",
        status=status,
    )


class _Interpret(OperationRequest):
    kind: Literal["test.interpret"] = "test.interpret"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Value
    evidence: tuple[EvidenceRef, ...]


class _Retain(OperationRequest):
    kind: Literal["test.retain"] = "test.retain"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Value
    revision: RevisionRef
    accuracy_proof: EvidenceRef


def _context() -> ExecutionContext:
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=1), now_at=1.0, payload_digest="d"
    )


# ledger


entries = st.lists(
    st.tuples(st.sampled_from(["s1", "s2"]), st.integers(0, 3), st.floats(0, 100)),
    min_size=1,
    max_size=8,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(items=entries, data=st.data())
def test_ledger_writes_are_idempotent_and_order_independent(
    items: list[tuple[str, int, float]], data: st.DataObject
) -> None:
    # One reading per (source, id): later duplicates repeat the first, never differ.
    unique = {(source, seed): value for source, seed, value in reversed(items)}
    writes = [(s, _evidence(seed, value=value)) for (s, seed), value in unique.items()]
    shuffled = data.draw(st.permutations(writes + writes))
    with tempfile.TemporaryDirectory() as raw:
        ledger = ReceiptEvidenceLedger(_store(Path(raw)))
        fake = FakeEvidenceLedger()
        for source, evidence in shuffled:
            for target in (ledger, fake):
                target.record(RequestId(root=source), evidence, "official")
        for source, evidence in writes:
            id_ = EvidenceId(root=evidence.evidence_id)
            got = ledger.lookup(_key(source, id_))
            assert got is not None
            assert got.evidence == evidence
            assert got.purpose == "official"
            assert fake.lookup(_key(source, id_)) == got


def test_a_conflicting_rewrite_is_rejected_and_the_first_reading_survives() -> None:
    with tempfile.TemporaryDirectory() as raw:
        for ledger in (ReceiptEvidenceLedger(_store(Path(raw))), FakeEvidenceLedger()):
            source = RequestId(root="s")
            ledger.record(source, _evidence(1, value=1.0), "official")
            with pytest.raises(ContractError):
                ledger.record(source, _evidence(1, value=2.0), "official")
            with pytest.raises(ContractError):
                ledger.record(source, _evidence(1, value=1.0), "baseline")
            got = ledger.lookup(_key("s", EvidenceId(root=_evidence(1).evidence_id)))
            assert got is not None
            assert got.evidence == _evidence(1, value=1.0)
            assert got.purpose == "official"


def test_the_same_evidence_id_from_another_request_is_another_entry() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger = ReceiptEvidenceLedger(_store(Path(raw)))
        ledger.record(RequestId(root="a"), _evidence(1, value=1.0), "official")
        ledger.record(RequestId(root="b"), _evidence(1, value=2.0), "official")
        id_ = EvidenceId(root=_evidence(1).evidence_id)
        first = ledger.lookup(_key("a", id_))
        second = ledger.lookup(_key("b", id_))
        assert first is not None
        assert second is not None
        assert first != second
        assert ledger.lookup(_key("c", id_)) is None


# interpret


async def test_evaluation_evidence_is_read_back_after_a_restart() -> None:
    cluster = ScenarioCluster()
    cluster.benchmark_exit = 1
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        stack = await build_stack(base / "stack", cluster)
        try:
            store = _store(base)
            requests = MeasurementRequests(stack.executor, store)
            sub = submission("sub", candidate=stack.snapshot)
            first = await requests.execute(sub, _eval_context(sub))
            resource = first.observation.observation.resource_id
            assert resource is not None
            while resource.root not in cluster.submissions:
                await stack.executor.wait_for_change(resource.root, 30.0)
            inspect = _inspect(resource)
            target = (await requests.execute(inspect, _eval_context(inspect))).observation.target
            assert target is not None
            assert len(target.evidence) == 2
            # A replayed poll sees the same terminal job and records nothing different.
            again = (await requests.execute(inspect, _eval_context(inspect))).observation.target
            assert again is not None
            refs = target.evidence
        finally:
            await stack.executor.close()
        # Restart: a new store over the same durable state, no executor.
        owner = InterpretEvidenceOwner(ReceiptEvidenceLedger(_store(base)))
        outcome = await owner.execute(_Interpret(evidence=refs), _context())
        assert outcome["status"] == "succeeded"
        readings = outcome["readings"]
        assert isinstance(readings, tuple)
        assert [r["stage"] for r in readings] == [r.stage_name for r in _stored(base, refs)]  # type: ignore[index]
        by_stage = {r["stage"]: r for r in readings}  # type: ignore[index]
        assert by_stage["accuracy"]["passed"] is True
        assert by_stage["benchmark"]["passed"] is False
        assert [r["kind"] for r in readings] == [ref.kind for ref in refs]  # type: ignore[index]
        assert [r["evidence_id"] for r in readings] == [ref.evidence_id for ref in refs]  # type: ignore[index]


def _stored(base: Path, refs: tuple[EvidenceRef, ...]) -> list[TrustedEvidence]:
    ledger = ReceiptEvidenceLedger(_store(base))
    found = [ledger.lookup(ref.key) for ref in refs]
    assert all(entry is not None for entry in found)
    return [entry.evidence for entry in found if entry is not None]


def _inspect(resource: ResourceId) -> InspectOwnedJob:
    from tests.support.runtime_evaluation import ADMISSION  # noqa: PLC0415

    return InspectOwnedJob(
        request_id=RequestId(root="inspect"),
        scope=SCOPE,
        admission_id=ADMISSION,
        deadline_at=100.0,
        resource_id=resource,
    )


class _Lease:
    def renew(self, *, now_at: float, lease_duration: float) -> None:
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        del now_at
        return True


def _eval_context(request: BaseModel) -> ExecutionContext:
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        now_at=5.0,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lease=_Lease(),
    )


@settings(max_examples=40, deadline=None)
@given(
    recorded=st.sets(st.integers(0, 4)), asked=st.lists(st.integers(0, 4), min_size=1, max_size=4)
)
async def test_interpret_reads_exactly_the_recorded_evidence_or_refuses(
    recorded: set[int], asked: list[int]
) -> None:
    ledger = FakeEvidenceLedger()
    for seed in recorded:
        ledger.record(RequestId(root="s"), _evidence(seed, value=float(seed)), "official")
    refs = tuple(_ref("s", _evidence(seed)) for seed in asked)
    outcome = await InterpretEvidenceOwner(ledger).execute(_Interpret(evidence=refs), _context())
    if set(asked) <= recorded:
        assert outcome["status"] == "succeeded"
        readings = outcome["readings"]
        assert isinstance(readings, tuple)
        assert [r["metrics"][0]["value"] for r in readings] == [float(s) for s in asked]  # type: ignore[index]
    else:
        assert outcome == {"status": "rejected", "readings": ()}


async def test_interpret_refuses_evidence_recorded_for_another_purpose() -> None:
    ledger = FakeEvidenceLedger()
    ledger.record(RequestId(root="s"), _evidence(1), "baseline")
    owner = InterpretEvidenceOwner(ledger)
    ref = _ref("s", _evidence(1), purpose="official")
    outcome = await owner.execute(_Interpret(evidence=(ref,)), _context())
    assert outcome["status"] == "rejected"


async def test_interpret_rejects_a_request_without_embedded_refs_naming_the_field() -> None:
    class Bare(OperationRequest):
        kind: Literal["test.interpret"] = "test.interpret"
        lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
        outcome_model: ClassVar[type[BaseModel]] = Value
        evidence: tuple[EvidenceId, ...]

    owner = InterpretEvidenceOwner(FakeEvidenceLedger())
    with pytest.raises(ContractError, match="evidence"):
        await owner.execute(Bare(evidence=(EvidenceId(root="e"),)), _context())


# retain


def _retain_owner(
    retained: set[str], known: set[str]
) -> tuple[RetainRevisionOwner, FakeWorkspace, FakeWorkspaces]:
    workspace = FakeWorkspace()
    workspaces = FakeWorkspaces(workspace)
    for revision in known - retained:
        workspaces.add_dangling_revision(revision)
    for revision in retained:
        workspaces.retain_candidate_revision(revision)
    return RetainRevisionOwner(workspaces, workspaces, commit_of, "verified"), workspace, workspaces


proof_kinds = st.sampled_from([EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK])
proof_status = st.sampled_from([ObservationStatus.SUCCEEDED, ObservationStatus.FAILED])
C1, C2, C3 = ("1" * 40, "2" * 40, "3" * 40)
revisions = st.sampled_from([C1, C2, C3])


@settings(max_examples=60, deadline=None)
@given(
    kind=proof_kinds,
    status=proof_status,
    proof_of=revisions,
    asked=revisions,
    known=st.sets(revisions),
)
async def test_retention_requires_a_successful_correctness_proof_of_exactly_that_revision(
    kind: EvidenceKind, status: ObservationStatus, proof_of: str, asked: str, known: set[str]
) -> None:
    owner, workspace, _ = _retain_owner(set(), known)
    proof = _ref("s", _evidence(1), kind=kind, status=status, candidate=proof_of)
    request = _Retain(revision=revision_ref(asked), accuracy_proof=proof)
    outcome = await owner.execute(request, _context())
    valid = (
        kind is EvidenceKind.CORRECTNESS
        and status is ObservationStatus.SUCCEEDED
        and proof_of == asked
    )
    assert outcome["retained"] is (valid and asked in known)
    assert (outcome["status"] == "succeeded") is outcome["retained"]
    assert (asked in workspace.retained.values()) is outcome["retained"]
    if not valid:
        assert outcome["detail"] in {"not_accuracy_proof", "proof_names_other_revision"}


async def test_retention_is_idempotent_and_inspection_proves_it() -> None:
    owner, workspace, _ = _retain_owner(set(), {C1})
    proof = _ref("s", _evidence(1), kind=EvidenceKind.CORRECTNESS, candidate=C1)
    request = _Retain(revision=revision_ref(C1), accuracy_proof=proof)
    assert isinstance(await owner.inspect(request, _context()), NotApplied)
    first = await owner.execute(request, _context())
    second = await owner.execute(request, _context())
    assert first == second
    assert first["retained"] is True
    assert list(workspace.retained.values()) == [C1]
    applied = await owner.inspect(request, _context())
    assert isinstance(applied, Applied)
    assert applied.outcome == first


async def test_retention_refuses_a_proof_core_never_accepted() -> None:
    owner, workspace, _ = _retain_owner(set(), {C1})
    proof = _ref("s", _evidence(1), kind=EvidenceKind.CORRECTNESS, candidate=C1, accepted=False)
    outcome = await owner.execute(
        _Retain(revision=revision_ref(C1), accuracy_proof=proof), _context()
    )
    assert outcome["retained"] is False
    assert outcome["detail"] == "not_accuracy_proof"
    assert not workspace.retained


async def test_retention_refuses_a_noncanonical_revision_reference() -> None:
    owner, _, _ = _retain_owner(set(), {C1})
    bad = RevisionRef(revision_id=RevisionId(root=C1), digest="sha256:other")
    proof = _ref("s", _evidence(1), kind=EvidenceKind.CORRECTNESS).model_copy(
        update={"candidate": bad}
    )
    outcome = await owner.execute(_Retain(revision=bad, accuracy_proof=proof), _context())
    assert outcome["retained"] is False
    assert outcome["detail"] == "revision_not_canonical"


# production wiring


def _registration(
    kind: str, request: type[OperationRequest], lifecycle: LifecycleClass
) -> OperationRegistration:
    stem = kind.replace(".", "-")
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=kind,
            request_schema=SchemaRef(name=stem, version=1),
            outcome_schema=SchemaRef(name=f"{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=lifecycle is not LifecycleClass.QUERY,
        ),
        request_model=request,
        outcome_model=Value,
    )


def test_a_strategy_requiring_interpretation_starts_only_with_production_owners() -> None:
    registrations = {
        OperationRole.INTERPRET_EVIDENCE: _registration(
            "test.interpret", _Interpret, LifecycleClass.QUERY
        ),
        OperationRole.RETAIN_REVISION: _registration(
            "test.retain", _Retain, LifecycleClass.IDEMPOTENT_WRITE
        ),
    }
    interpret, retain = (r.descriptor for r in registrations.values())
    ports = _ports()
    declaration = StrategyDeclaration(
        strategy_id=StrategyId(root="s"),
        state_schema=SchemaRef(name="state", version=1),
        required_operations=(_schema(interpret),),
        optional_operations=(_schema(retain),),
    )
    owned = build_operation_catalog(registrations, production_owners(registrations, ports))
    owned.require_owned(declaration)
    assert {d.kind for d in owned.offered_operations} == {"test.interpret", "test.retain"}
    validate_startup(declaration, Capabilities(operations=owned.offered_operations))
    ownerless = build_operation_catalog(registrations, {})
    with pytest.raises(ContractError, match=r"test\.interpret"):
        ownerless.require_owned(declaration)


def _schema(descriptor: OperationDescriptor) -> OperationSchemaRef:
    return OperationSchemaRef(
        kind=descriptor.kind,
        request_schema=descriptor.request_schema,
        outcome_schema=descriptor.outcome_schema,
        lifecycle=descriptor.lifecycle,
    )


def _ports() -> OperationPorts:
    workspaces = FakeWorkspaces(FakeWorkspace())
    return OperationPorts(
        renderer=None,  # type: ignore[arg-type]  # roles under test never render
        artifacts=None,  # type: ignore[arg-type]
        workspaces=workspaces,
        ledger=workspaces,
        evidence=FakeEvidenceLedger(),
        commit_of=commit_of,
        retention_label="verified",
    )
