"""One admission contract runs unchanged against retained file and Fake receipts."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import (
    AdmissionBusyError,
    AdmissionConflictError,
    AdmissionUnconfirmedError,
    FileAdmissionStore,
    LocalAtomicWriteEffects,
    Project,
)
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import FailureCategory, Oneof, OperationFailure, ValidationError, admission
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import AdmissionClaim, AdmissionStore, AtomicWriteStream


def _identity(request_id: str = "request-a") -> admission.AdmissionIdentity:
    return admission.AdmissionIdentity(
        operation_id=10, request_id=request_id, host_user_target="host-a"
    )


def _request(
    request_id: str = "request-a", project: str = "/project"
) -> admission.AcceptedOperation:
    return admission.AcceptedOperation(
        request=Oneof("list_tasks", pb.ListTasksRequest(request_id=request_id, project=project))
    )


def _intent(
    intent: admission.PublicationEvidence | None,
) -> admission.PublicationEvidence:
    """An independent copy of the stored intent an observation must name."""
    assert intent is not None
    return admission.PublicationEvidence.from_binary(intent.to_binary())


def _observation(
    kind: str,
    intent: admission.PublicationEvidence,
    result: admission.OperationResult | None = None,
) -> admission.PublicationObservation:
    """Inspection evidence for one exact fenced intent."""
    named = _intent(intent)
    if kind == "absent":
        body = Oneof("absent", admission.PublicationAbsent(intent=named))
    elif kind == "unknown":
        body = Oneof("unknown", admission.PublicationUnknown(intent=named))
    else:
        assert result is not None
        body = Oneof("published", admission.PublicationPublished(intent=named, result=result))
    return admission.PublicationObservation(observation=body)


def _result(request_id: str = "request-a") -> admission.OperationResult:
    return admission.OperationResult(
        result=Oneof(
            "list_tasks",
            pb.ListTasksResponse(
                request_id=request_id, tasks=pb.TaskList(version=1, project_root="/project")
            ),
        )
    )


@pytest.fixture(params=["file", "fake"])
def reopen(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], AdmissionStore]:
    if request.param == "file":
        return lambda: FileAdmissionStore(tmp_path / "receipts")
    store = FakeAdmissionStore()
    return lambda: store


def test_a_claim_excludes_same_identity_and_overlapping_targets(
    reopen: Callable[[], AdmissionStore],
) -> None:
    """Concurrent equal requests and different ids on one target admit no second owner."""
    first, second = reopen(), reopen()
    with first.claim(_identity(), ("/target-a", "/target-b"), _request()) as owner:
        with (
            pytest.raises(AdmissionBusyError),
            second.claim(_identity(), ("/target-b", "/target-a"), _request()),
        ):
            pytest.fail("duplicate owner")
        with (
            pytest.raises(AdmissionBusyError),
            second.claim(_identity("request-b"), ("/target-b",), _request("request-b")),
        ):
            pytest.fail("overlapping owner")
        with second.claim(_identity("request-c"), ("/unrelated",), _request("request-c")):
            owner.record_intent(admission.PublicationEvidence(effect_id="publication-a"))
            owner.settle(admission.AdmissionStatus.CONFIRMED, _result())
    with second.claim(_identity(), ("/target-b", "/target-a"), _request()) as retry:
        assert retry.observe().result == _result()
        with pytest.raises(AdmissionUnconfirmedError):
            retry.record_intent(admission.PublicationEvidence(effect_id="publication-b"))


def test_exact_bytes_targets_and_host_identity_cannot_be_reused(
    reopen: Callable[[], AdmissionStore],
) -> None:
    store = reopen()
    with store.claim(_identity(), ("/target-a",), _request()):
        pass
    for targets, payload in [
        (("/target-a",), _request(project="/different")),
        (("/target-b",), _request()),
    ]:
        with pytest.raises(AdmissionConflictError), reopen().claim(_identity(), targets, payload):
            pytest.fail("identity reused")
    changed = _identity()
    changed.host_user_target = "host-b"
    with pytest.raises(ValueError, match="identity mismatch"):
        reopen().lookup(changed)


@pytest.mark.parametrize("boundary", ["identity", "accepted", "evidence", "result", "observation"])
def test_unknown_admission_metadata_is_rejected_before_publication(
    reopen: Callable[[], AdmissionStore], boundary: str
) -> None:
    unknown = b"\x98\x06\x01"  # Unknown field 99, varint 1.
    store = reopen()
    if boundary in ("identity", "accepted"):
        identity, accepted = _identity(), _request()
        if boundary == "identity":
            identity = admission.AdmissionIdentity.from_binary(identity.to_binary() + unknown)
        else:
            accepted = admission.AcceptedOperation.from_binary(accepted.to_binary() + unknown)
        with (
            pytest.raises(ValueError, match="unknown fields"),
            store.claim(identity, ("/target",), accepted),
        ):
            pytest.fail("unknown metadata was admitted")
        assert store.lookup(_identity()) is None
        return
    with store.claim(_identity(), ("/target",), _request()) as claim:
        evidence = admission.PublicationEvidence(effect_id="effect-a")
        if boundary == "evidence":
            evidence = admission.PublicationEvidence.from_binary(evidence.to_binary() + unknown)
            before = claim.observe()
            with pytest.raises(ValueError, match="unknown fields"):
                claim.record_intent(evidence)
        else:
            claim.record_intent(evidence)
            before = claim.observe()
            if boundary == "result":
                result = admission.OperationResult.from_binary(_result().to_binary() + unknown)
                with pytest.raises(ValueError, match="unknown fields"):
                    claim.settle(admission.AdmissionStatus.CONFIRMED, result)
            else:
                assert before.intent is not None
                observation = _observation("absent", before.intent)
                unrecognized = admission.PublicationObservation.from_binary(
                    observation.to_binary() + unknown
                )
                with pytest.raises(ValueError, match="unknown fields"):
                    claim.reconcile(unrecognized)
        assert claim.observe() == before
        assert store.lookup(_identity()) == before


def test_intent_survives_disconnect_and_never_redispatches(
    reopen: Callable[[], AdmissionStore],
) -> None:
    with reopen().claim(_identity(), ("/target",), _request()) as owner:
        owner.record_intent(
            admission.PublicationEvidence(
                effect_id="launch-fence", process_identity="stable-start-identity"
            )
        )
    with reopen().claim(_identity(), ("/target",), _request()) as recovered:
        observed = recovered.observe()
        assert observed.intent is not None
        assert observed.intent.process_identity == "stable-start-identity"
        with pytest.raises(AdmissionUnconfirmedError):
            recovered.record_intent(admission.PublicationEvidence(effect_id="second-launch"))
        recovered.settle(admission.AdmissionStatus.CONFIRMED, _result())
    found = reopen().lookup(_identity())
    assert found is not None
    assert found.result == _result()


def test_terminal_result_is_retained_and_copies_do_not_mutate_it(
    reopen: Callable[[], AdmissionStore],
) -> None:
    with reopen().claim(_identity(), ("/target",), _request()) as owner:
        with pytest.raises(ValueError, match="durable intent"):
            owner.settle(admission.AdmissionStatus.CONFIRMED, _result())
        owner.record_intent(admission.PublicationEvidence(effect_id="effect-a"))
        owner.settle(admission.AdmissionStatus.CONFIRMED, _result())
        owner.settle(admission.AdmissionStatus.CONFIRMED, _result())
        copy = owner.observe()
        assert copy.identity is not None
        copy.identity.request_id = "changed"
        with pytest.raises(AdmissionConflictError):
            owner.settle(admission.AdmissionStatus.REFUSED, _result())
    with pytest.raises(RuntimeError, match="closed"):
        owner.observe()
    found = reopen().lookup(_identity())
    assert found is not None
    assert found.identity == _identity()
    with reopen().claim(_identity(), ("/target",), _request()) as retried:
        assert retried.observe().result == _result()


@given(
    project=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40)
)
def test_every_changed_input_conflicts(project: str) -> None:
    store = FakeAdmissionStore()
    with store.claim(_identity(), ("/target",), _request(project=project)):
        pass
    with (
        pytest.raises(AdmissionConflictError),
        store.claim(_identity(), ("/target",), _request(project=project + "changed")),
    ):
        pytest.fail("changed input admitted")


class _FaultyEffects(LocalAtomicWriteEffects):
    """Real durability operations wrapped by the shared deterministic fault scheduler."""

    def __init__(self, gate: FaultGate) -> None:
        self._gate = gate

    def sync_file(self, stream: AtomicWriteStream) -> None:
        self._gate.around(
            Boundary.DURABLE_WRITE,
            "file-sync",
            lambda: super(_FaultyEffects, self).sync_file(stream),
        )

    def replace(self, temporary: Path, destination: Path) -> None:
        self._gate.around(
            Boundary.DURABLE_WRITE,
            "rename",
            lambda: super(_FaultyEffects, self).replace(temporary, destination),
        )

    def sync_directory(self, directory: Path) -> None:
        self._gate.around(
            Boundary.DURABLE_WRITE,
            "directory-sync",
            lambda: super(_FaultyEffects, self).sync_directory(directory),
        )


@pytest.mark.parametrize("boundary", ["file-sync", "rename", "directory-sync"])
@pytest.mark.parametrize("ordinal", [1, 2, 3])
def test_a_crash_at_each_receipt_publication_preserves_single_effect(
    tmp_path: Path, boundary: str, ordinal: int
) -> None:
    """Crash in pending/intent/confirmation persistence, then reconcile original evidence."""
    gate = FaultGate(
        FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.DURABLE_WRITE,
                    target=boundary,
                    at=ordinal,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )
    )
    root = tmp_path / "receipts"
    store = FileAdmissionStore(root, effects=_FaultyEffects(gate))
    effects = []
    try:
        with store.claim(_identity(), ("/target",), _request()) as owner:
            owner.record_intent(admission.PublicationEvidence(effect_id="effect-a"))
            effects.append("effect-a")
            owner.settle(admission.AdmissionStatus.CONFIRMED, _result())
    except HostCrashError:
        pass
    recovered = FileAdmissionStore(root)
    with recovered.claim(_identity(), ("/target",), _request()) as claim:
        receipt = claim.observe()
        if receipt.result is not None:
            assert effects == ["effect-a"]
        elif receipt.intent is not None:
            with pytest.raises(AdmissionUnconfirmedError):
                claim.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
            if effects:
                claim.settle(admission.AdmissionStatus.CONFIRMED, _result())
        else:
            claim.record_intent(admission.PublicationEvidence(effect_id="effect-a"))
            effects.append("effect-a")
            claim.settle(admission.AdmissionStatus.CONFIRMED, _result())
    assert len(effects) <= 1


def test_project_admissions_survive_project_rename_and_need_no_project(tmp_path: Path) -> None:
    store = Project.admissions(home=tmp_path)
    with store.claim(_identity(), (str(tmp_path / "project"),), _request()):
        pass
    assert Project.admissions(home=tmp_path).lookup(_identity()) is not None


def test_reconciliation_keeps_unknown_effects_and_fences_proven_absence(
    reopen: Callable[[], AdmissionStore],
) -> None:
    with reopen().claim(_identity(), ("/target",), _request()) as claim:
        claim.record_intent(admission.PublicationEvidence(effect_id="effect-a"))
        first = claim.observe()
        assert first.intent is not None
        assert first.intent.fence == first.fence
        recorded = claim.observe().intent
        assert recorded is not None
        claim.reconcile(_observation("unknown", recorded))
        assert claim.observe().status == admission.AdmissionStatus.UNCONFIRMED
        with pytest.raises(AdmissionUnconfirmedError):
            claim.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
    with reopen().claim(_identity(), ("/target",), _request()) as recovered:
        assert recovered.observe().intent is not None
        with pytest.raises(AdmissionConflictError, match="fenced intent"):
            recovered.reconcile(
                _observation(
                    "absent",
                    admission.PublicationEvidence(
                        effect_id="other", fence=recovered.observe().fence
                    ),
                )
            )
        current = recovered.observe().intent
        assert current is not None
        recovered.reconcile(_observation("absent", current))
        assert recovered.observe().fence == first.fence + 1
        recovered.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
        published = recovered.observe().intent
        assert published is not None
        recovered.reconcile(_observation("published", published, _result()))
    final = reopen().lookup(_identity())
    assert final is not None
    assert final.status == admission.AdmissionStatus.CONFIRMED
    with reopen().claim(_identity(), ("/target",), _request()) as terminal:
        terminal_intent = terminal.observe().intent
        assert terminal_intent is not None
        with pytest.raises(AdmissionConflictError, match="terminal"):
            terminal.reconcile(_observation("absent", terminal_intent))


def test_unconfirmed_and_refused_results_are_typed_and_never_start_an_effect(
    reopen: Callable[[], AdmissionStore],
) -> None:
    refusal = admission.OperationResult(
        result=Oneof(
            "failure",
            OperationFailure(
                category=FailureCategory.PERMISSION_DENIED, message="read-only source"
            ),
        )
    )
    with reopen().claim(_identity(), ("/settings",), _request()) as claim:
        claim.settle(admission.AdmissionStatus.REFUSED, refusal)
    with reopen().claim(_identity(), ("/settings",), _request()) as retried:
        assert retried.observe().result == refusal
        with pytest.raises(AdmissionUnconfirmedError):
            retried.record_intent(admission.PublicationEvidence(effect_id="forbidden"))


def test_claims_own_identity_input_evidence_and_terminal_result_copies(
    reopen: Callable[[], AdmissionStore],
) -> None:
    identity, request = _identity(), _request()
    evidence = admission.PublicationEvidence(effect_id="effect-a")
    result = _result()
    with reopen().claim(identity, ("/target",), request) as claim:
        identity.request_id = "changed"
        assert request.request is not None
        assert isinstance(request.request.value, pb.ListTasksRequest)
        request.request.value.project = "/changed"
        claim.record_intent(evidence)
        evidence.effect_id = "changed"
        claim.settle(admission.AdmissionStatus.CONFIRMED, result)
        assert result.result is not None
        assert isinstance(result.result.value, pb.ListTasksResponse)
        result.result.value.request_id = "changed"
        observed = claim.observe()
        assert observed.identity == _identity()
        assert observed.accepted == _request()
        assert observed.intent is not None
        assert observed.intent.effect_id == "effect-a"
        assert observed.result == _result()
    stored = reopen().lookup(_identity())
    assert stored is not None
    assert stored.result == _result()


@pytest.mark.parametrize("crossing", ["intent", "effect", "confirmation"])
def test_a_crash_after_an_admission_boundary_never_repeats_the_effect(
    reopen: Callable[[], AdmissionStore], crossing: str
) -> None:
    gate = FaultGate(
        FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.EXECUTOR_REQUEST,
                    target=crossing,
                    at=1,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )
    )
    published = []
    try:
        with reopen().claim(_identity(), ("/target",), _request()) as claim:
            gate.around(
                Boundary.EXECUTOR_REQUEST,
                "intent",
                lambda: claim.record_intent(admission.PublicationEvidence(effect_id="effect-a")),
            )
            gate.around(Boundary.EXECUTOR_REQUEST, "effect", lambda: published.append("effect-a"))
            gate.around(
                Boundary.EXECUTOR_REQUEST,
                "confirmation",
                lambda: claim.settle(admission.AdmissionStatus.CONFIRMED, _result()),
            )
    except HostCrashError:
        pass
    with reopen().claim(_identity(), ("/target",), _request()) as recovered:
        if recovered.observe().result is None:
            intent = recovered.observe().intent
            assert intent is not None
            kind = "published" if published else "unknown"
            recovered.reconcile(_observation(kind, intent, _result() if published else None))
        with pytest.raises(AdmissionUnconfirmedError):
            recovered.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
    assert len(published) <= 1


def _unresolved(receipt: admission.AdmissionReceipt | None) -> bool:
    return (
        receipt is not None
        and receipt.intent is not None
        and receipt.status
        in (
            admission.AdmissionStatus.PENDING,
            admission.AdmissionStatus.UNCONFIRMED,
        )
    )


@pytest.mark.regression
def test_a_stale_absence_observation_does_not_clear_a_newer_intent(
    reopen: Callable[[], AdmissionStore],
) -> None:
    """A fence-1 absence, and any other field change, leaves the newer intent in place."""
    store = reopen()
    with store.claim(_identity(), ("/target",), _request()) as claim:
        claim.record_intent(
            admission.PublicationEvidence(effect_id="effect-e", process_identity="proc-0")
        )
        first = _intent(claim.observe().intent)
        with pytest.raises(ValidationError):
            claim.reconcile(
                _observation("absent", admission.PublicationEvidence(effect_id="effect-e", fence=0))
            )
        forged = _intent(first)
        forged.process_identity = "other-process"
        with pytest.raises(AdmissionConflictError, match="fenced intent"):
            claim.reconcile(_observation("absent", forged))
        assert claim.observe().intent == first
        claim.reconcile(_observation("absent", first))
        assert claim.observe().fence == first.fence + 1
        claim.record_intent(
            admission.PublicationEvidence(effect_id="effect-e", process_identity="proc-0")
        )
        second = claim.observe()
        assert second.intent is not None
        assert second.intent.fence == first.fence + 1
        with pytest.raises(AdmissionConflictError, match="fenced intent"):
            claim.reconcile(_observation("absent", first))
        assert claim.observe() == second


@pytest.mark.regression
def test_an_unresolved_intent_occupies_its_target_after_the_claim_exits(
    reopen: Callable[[], AdmissionStore],
) -> None:
    """Losing the reply and leaving the scope does not let another request take the target."""
    store = reopen()
    with store.claim(_identity("request-a"), ("/target",), _request("request-a")) as claim:
        claim.record_intent(admission.PublicationEvidence(effect_id="live-a"))
    with (
        pytest.raises(AdmissionBusyError),
        store.claim(_identity("request-b"), ("/target",), _request("request-b")),
    ):
        pytest.fail("overlapping unresolved intent")
    kept = store.lookup(_identity("request-a"))
    assert kept is not None
    assert kept.intent is not None
    assert kept.intent.effect_id == "live-a"
    assert store.lookup(_identity("request-b")) is None
    with store.claim(_identity("request-a"), ("/target",), _request("request-a")) as claim:
        current = claim.observe().intent
        assert current is not None
        claim.reconcile(_observation("absent", current))
    with store.claim(_identity("request-b"), ("/target",), _request("request-b")) as other:
        other.record_intent(admission.PublicationEvidence(effect_id="live-b"))


_HISTORY_OPS = ("record", "absent", "unknown", "published", "settle", "exit")
_HISTORY_REQUESTS = ("request-a", "request-b")
_HISTORY_TARGETS = (("/target-0",), ("/target-1",), ("/target-0", "/target-1"))
_Step = tuple[str, str, tuple[str, ...], str, str]


def _history_store(kind: str, root: Path) -> AdmissionStore:
    if kind == "file":
        return FileAdmissionStore(root / "receipts")
    return FakeAdmissionStore()


def _assert_one_unresolved_intent_per_target(store: AdmissionStore) -> None:
    occupied: dict[str, str] = {}
    for request_id in _HISTORY_REQUESTS:
        receipt = store.lookup(_identity(request_id))
        if not _unresolved(receipt):
            continue
        assert receipt is not None
        for target in receipt.targets:
            assert target not in occupied
            occupied[target] = request_id


def _replay_stale(
    store: AdmissionStore,
    request_id: str,
    targets: tuple[str, ...],
    old: admission.PublicationEvidence,
) -> None:
    identity = _identity(request_id)
    before = store.lookup(identity)
    try:
        with (
            store.claim(identity, targets, _request(request_id)) as claim,
            pytest.raises(AdmissionConflictError),
        ):
            claim.reconcile(_observation("absent", old))
    except AdmissionBusyError:
        pass
    assert store.lookup(identity) == before


def _reject_stale_observations(
    store: AdmissionStore,
    chosen: dict[str, tuple[str, ...]],
    past: dict[str, list[admission.PublicationEvidence]],
) -> None:
    for request_id, snapshots in past.items():
        current = store.lookup(_identity(request_id))
        current_intent = None if current is None else current.intent
        for old in snapshots:
            if current_intent is not None and old == current_intent:
                continue
            _replay_stale(store, request_id, chosen[request_id], old)


def _record_if_absent(
    claim: AdmissionClaim,
    request_id: str,
    effect: str,
    marker: str,
    past: dict[str, list[admission.PublicationEvidence]],
) -> None:
    receipt = claim.observe()
    if receipt.status != admission.AdmissionStatus.PENDING or receipt.intent is not None:
        return
    claim.record_intent(admission.PublicationEvidence(effect_id=effect, process_identity=marker))
    recorded = claim.observe().intent
    assert recorded is not None
    past.setdefault(request_id, []).append(_intent(recorded))


def _open_intent(
    receipt: admission.AdmissionReceipt | None,
) -> admission.PublicationEvidence | None:
    if not _unresolved(receipt) or receipt is None:
        return None
    return receipt.intent


def _finish_if_unresolved(claim: AdmissionClaim, op: str, request_id: str) -> None:
    receipt = claim.observe()
    intent = _open_intent(receipt)
    if intent is None or receipt is None:
        return
    if op == "settle":
        if receipt.status != admission.AdmissionStatus.PENDING:
            return
        claim.settle(admission.AdmissionStatus.CONFIRMED, _result(request_id))
        return
    result = _result(request_id) if op == "published" else None
    claim.reconcile(_observation(op, intent, result))


def _apply_history_step(
    store: AdmissionStore,
    chosen: dict[str, tuple[str, ...]],
    past: dict[str, list[admission.PublicationEvidence]],
    step: _Step,
) -> None:
    op, request_id, targets, effect, marker = step
    identity = _identity(request_id)
    known = chosen.get(request_id)
    if known is not None and known != targets:
        before = store.lookup(identity)
        with (
            pytest.raises((AdmissionConflictError, AdmissionBusyError)),
            store.claim(identity, targets, _request(request_id)),
        ):
            pytest.fail("changed targets admitted")
        assert store.lookup(identity) == before
        return
    try:
        with store.claim(identity, targets, _request(request_id)) as claim:
            chosen.setdefault(request_id, targets)
            if op == "record":
                _record_if_absent(claim, request_id, effect, marker, past)
            elif op != "exit":
                _finish_if_unresolved(claim, op, request_id)
    except AdmissionBusyError:
        return


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["file", "fake"])
@settings(max_examples=examples(pr=6, nightly=24))
@given(
    steps=st.lists(
        st.tuples(
            st.sampled_from(_HISTORY_OPS),
            st.sampled_from(_HISTORY_REQUESTS),
            st.sampled_from(_HISTORY_TARGETS),
            st.sampled_from(("effect-a", "effect-b")),
            st.sampled_from(("proc-0", "proc-1")),
        ),
        min_size=1,
        max_size=8,
    )
)
@example(
    steps=[
        ("record", "request-a", ("/target-0",), "effect-e", "proc-0"),
        ("absent", "request-a", ("/target-0",), "effect-e", "proc-0"),
        ("record", "request-a", ("/target-0",), "effect-e", "proc-0"),
    ]
)
@example(
    steps=[
        ("record", "request-a", ("/target-0",), "live-a", "proc-0"),
        ("record", "request-b", ("/target-0",), "live-b", "proc-1"),
    ]
)
@example(
    steps=[
        ("record", "request-a", ("/target-1",), "effect-a", "proc-0"),
        ("record", "request-a", ("/target-0",), "effect-a", "proc-0"),
        ("record", "request-b", ("/target-0",), "effect-a", "proc-0"),
        ("record", "request-a", ("/target-0",), "effect-a", "proc-0"),
    ]
)
def test_crash_histories_keep_target_exclusion_and_reject_stale_observations(
    kind: str, steps: list[_Step]
) -> None:
    """Interleaved intents, lost replies, scope exits and stale observations."""
    with TemporaryDirectory() as directory:
        store = _history_store(kind, Path(directory))
        chosen: dict[str, tuple[str, ...]] = {}
        past: dict[str, list[admission.PublicationEvidence]] = {}
        for step in steps:
            _apply_history_step(store, chosen, past, step)
            _assert_one_unresolved_intent_per_target(store)
            _reject_stale_observations(store, chosen, past)
