"""One admission contract runs unchanged against retained file and Fake receipts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
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
from vs_proto.api import FailureCategory, Oneof, OperationFailure, admission
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_project.api import AdmissionStore, AtomicWriteStream


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
        claim.reconcile(
            admission.PublicationObservation(
                observation=Oneof("unknown", admission.PublicationUnknown(effect_id="effect-a"))
            )
        )
        assert claim.observe().status == admission.AdmissionStatus.UNCONFIRMED
        with pytest.raises(AdmissionUnconfirmedError):
            claim.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
    with reopen().claim(_identity(), ("/target",), _request()) as recovered:
        with pytest.raises(AdmissionConflictError, match="original intent"):
            recovered.reconcile(
                admission.PublicationObservation(
                    observation=Oneof("absent", admission.PublicationAbsent(effect_id="other"))
                )
            )
        recovered.reconcile(
            admission.PublicationObservation(
                observation=Oneof("absent", admission.PublicationAbsent(effect_id="effect-a"))
            )
        )
        assert recovered.observe().fence == first.fence + 1
        recovered.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
        recovered.reconcile(
            admission.PublicationObservation(
                observation=Oneof(
                    "published",
                    admission.PublicationPublished(effect_id="effect-b", result=_result()),
                )
            )
        )
    final = reopen().lookup(_identity())
    assert final is not None
    assert final.status == admission.AdmissionStatus.CONFIRMED
    with (
        reopen().claim(_identity(), ("/target",), _request()) as terminal,
        pytest.raises(AdmissionConflictError, match="terminal"),
    ):
        terminal.reconcile(
            admission.PublicationObservation(
                observation=Oneof("absent", admission.PublicationAbsent(effect_id="effect-b"))
            )
        )


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
            observation = (
                Oneof(
                    "published",
                    admission.PublicationPublished(effect_id="effect-a", result=_result()),
                )
                if published
                else Oneof("unknown", admission.PublicationUnknown(effect_id="effect-a"))
            )
            recovered.reconcile(admission.PublicationObservation(observation=observation))
        with pytest.raises(AdmissionUnconfirmedError):
            recovered.record_intent(admission.PublicationEvidence(effect_id="effect-b"))
    assert len(published) <= 1
