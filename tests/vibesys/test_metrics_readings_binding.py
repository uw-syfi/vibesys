"""Readings the evidence owner returns bind to the records asked about, whatever their order.

`test_metrics_readings.py` covers a reading of the wrong kind, of an unrequested id and a
repeat; these are the two cases it leaves out: two requested records sharing one evidence id
make a reading of that id ambiguous, and readings given in any order each bind to their own
source request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.metrics import AcceptedReading, EvidenceReading, accept_readings
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

if TYPE_CHECKING:
    from random import Random

_CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="d")


def _ref(name: str, source: str, kind: EvidenceKind = EvidenceKind.BENCHMARK) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=EvidenceId(root=name),
        kind=kind,
        purpose="official",
        scope=Scope(owner=RunId(root="run"), generation=0),
        source_request=RequestId(root=source),
        candidate=_CANDIDATE,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )


def _reading(ref: EvidenceRef) -> EvidenceReading:
    return EvidenceReading(evidence_id=ref.evidence_id, kind=ref.kind, passed=True, stage="s")


@settings(max_examples=examples(pr=8, nightly=100))
@given(sources=st.lists(st.sampled_from("abcdef"), unique=True, min_size=2, max_size=4))
def test_a_reading_that_two_requested_records_share_is_ambiguous_and_refused(
    sources: list[str],
) -> None:
    refs = tuple(_ref("same", f"request-{source}") for source in sources)

    assert isinstance(accept_readings((_reading(refs[0]),), refs), str)


@settings(max_examples=examples(pr=8, nightly=100))
@given(names=st.lists(st.sampled_from("abcdef"), unique=True, min_size=1), random=st.randoms())
def test_readings_in_any_order_bind_each_to_its_own_requested_record(
    names: list[str], random: Random
) -> None:
    refs = tuple(_ref(name, f"request-{name}") for name in names)
    readings = [_reading(ref) for ref in refs]
    random.shuffle(readings)

    accepted = accept_readings(tuple(readings), refs)

    assert isinstance(accepted, tuple)
    assert all(isinstance(item, AcceptedReading) for item in accepted)
    assert {item.key for item in accepted} == {ref.key for ref in refs}
    assert [item.evidence_id for item in accepted] == [item.evidence_id for item in readings]
