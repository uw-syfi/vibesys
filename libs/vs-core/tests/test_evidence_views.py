"""Evidence is offered to a decision only when it is trusted and is exactly the asked measurement's."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    MeasurementResult,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    Scope,
    initial_state,
    ledger_refs,
    project,
    trusted_keys,
)
from vs_sim.api.testing import examples

SCOPE = Scope(owner=RunId(root="run"), generation=0)
CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="digest-c")
OTHER = RevisionRef(revision_id=RevisionId(root="o"), digest="digest-o")
REQUEST = RequestId(root="request")


def _ref(name: str, **changes: object) -> EvidenceRef:
    ref = EvidenceRef(
        evidence_id=EvidenceId(root=name),
        kind=EvidenceKind.BENCHMARK,
        purpose="official",
        scope=SCOPE,
        source_request=REQUEST,
        candidate=CANDIDATE,
        observation_sequence=1,
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )
    return ref.model_copy(update=changes)


_FOREIGN = {"source_request": RequestId(root="foreign")}
_FLAWS = st.sampled_from(
    [
        {"provenance": "self-report"},
        {"candidate": OTHER},
        {"purpose": "baseline"},
        {"scope": Scope(owner=RunId(root="run"), generation=1)},
        _FOREIGN,
    ]
)


@given(flaws=st.lists(st.one_of(st.none(), _FLAWS), max_size=6), pinned=st.booleans())
def test_only_unflawed_records_of_the_measurement_are_kept(
    *, flaws: list[dict[str, object] | None], pinned: bool
) -> None:
    refs = tuple(_ref(f"e{i}", **(flaw or {})) for i, flaw in enumerate(flaws))
    result = MeasurementResult(
        scope=SCOPE,
        source_request=REQUEST if pinned else None,
        evidence=refs,
        status=ObservationStatus.SUCCEEDED,
    )
    kept = trusted_keys(result, scope=SCOPE, candidate=CANDIDATE, purpose="official")
    # Another request's record is dropped only when the result names its own request.
    expected = tuple(
        ref.key
        for ref, flaw in zip(refs, flaws, strict=True)
        if flaw is None or (flaw == _FOREIGN and not pinned)
    )
    assert kept == expected


@settings(max_examples=examples(pr=4, nightly=100))
@given(held=st.integers(0, 4), asked=st.integers(0, 4))
def test_the_ledger_must_hold_every_requested_record_in_order(held: int, asked: int) -> None:
    refs = tuple(_ref(f"e{i}") for i in range(max(held, asked)))
    view = project(initial_state()).model_copy(update={"measurements": refs[:held]})
    keys = tuple(ref.key for ref in refs[:asked])
    assert ledger_refs(view, keys) == (refs[:asked] if asked <= held else None)
