"""Decoded readings are accepted only for exactly the evidence records that were asked about."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.metrics import EvidenceReading, accept_readings, reading_of
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
)
from vs_sim.api.testing import examples

_CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="d")


def _ref(name: str, kind: EvidenceKind, status: ObservationStatus) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=EvidenceId(root=name),
        kind=kind,
        purpose="official",
        scope=Scope(owner=RunId(root="run"), generation=0),
        source_request=RequestId(root=f"request-{name}"),
        candidate=_CANDIDATE,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=status,
    )


def _reading(name: str, kind: EvidenceKind, *, passed: bool) -> EvidenceReading:
    return EvidenceReading(evidence_id=EvidenceId(root=name), kind=kind, passed=passed, stage="s")


_KINDS = st.sampled_from([EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK])


@settings(max_examples=examples(pr=8, nightly=100))
@given(kinds=st.lists(_KINDS, max_size=5))
def test_every_requested_record_is_bound_to_its_own_request(kinds: list[EvidenceKind]) -> None:
    refs = tuple(_ref(f"e{i}", kind, ObservationStatus.SUCCEEDED) for i, kind in enumerate(kinds))
    readings = tuple(_reading(f"e{i}", kind, passed=True) for i, kind in enumerate(kinds))
    accepted = accept_readings(readings, refs)
    assert not isinstance(accepted, str)
    assert [item.key for item in accepted] == [ref.key for ref in refs]


@settings(max_examples=examples(pr=8, nightly=100))
@given(kind=_KINDS)
def test_a_reading_of_another_kind_or_an_unrequested_id_or_a_repeat_is_refused(
    kind: EvidenceKind,
) -> None:
    other = EvidenceKind.BENCHMARK if kind is EvidenceKind.CORRECTNESS else EvidenceKind.CORRECTNESS
    ref = _ref("e", kind, ObservationStatus.SUCCEEDED)
    assert isinstance(accept_readings((_reading("e", other, passed=True),), (ref,)), str)
    assert isinstance(accept_readings((_reading("x", kind, passed=True),), (ref,)), str)
    twice = (_reading("e", kind, passed=True),) * 2
    assert isinstance(accept_readings(twice, (ref,)), str)


def test_a_pass_over_a_record_that_did_not_succeed_is_refused() -> None:
    ref = _ref("e", EvidenceKind.BENCHMARK, ObservationStatus.FAILED)
    assert isinstance(
        accept_readings((_reading("e", EvidenceKind.BENCHMARK, passed=True),), (ref,)), str
    )
    assert not isinstance(
        accept_readings((_reading("e", EvidenceKind.BENCHMARK, passed=False),), (ref,)), str
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(kinds=st.lists(_KINDS, min_size=1, max_size=5))
def test_the_latest_reading_of_a_kind_wins(kinds: list[EvidenceKind]) -> None:
    readings = tuple(_reading(f"e{i}", kind, passed=True) for i, kind in enumerate(kinds))
    for kind in set(kinds):
        found = reading_of(readings, kind)
        assert found is not None
        assert found.evidence_id.root == f"e{max(i for i, k in enumerate(kinds) if k is kind)}"
