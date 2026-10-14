"""A parent snapshot is a closed, immutable value that survives the record's JSON round trip.

The catalog of parents is published with the run's record and rebuilt from it after a restart, so
a snapshot must read back as exactly what was written, refuse a key nothing reads (a feature a
newer writer added, say) naming it, and never change after it is built.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic._values import snapshots

from vibesys.orchestration.dynamic.search.api import ParentSnapshot, ingest, options
from vs_sim.api.testing import examples


@settings(max_examples=examples(pr=8, nightly=100))
@given(row=snapshots())
def test_a_snapshot_and_the_catalog_of_it_read_back_exactly_as_written(
    row: ParentSnapshot,
) -> None:
    assert ParentSnapshot.model_validate_json(row.model_dump_json()) == row
    catalog = ingest((), row)
    again = tuple(ParentSnapshot.model_validate_json(item.model_dump_json()) for item in catalog)
    assert again == catalog
    assert options(again) == options(catalog)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    row=snapshots(),
    key=st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20).filter(
        lambda name: name not in ParentSnapshot.model_fields
    ),
)
def test_a_snapshot_refuses_a_key_it_does_not_have_naming_it(row: ParentSnapshot, key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        ParentSnapshot.model_validate({**row.model_dump(), key: ["batching"]})


@settings(max_examples=examples(pr=8, nightly=100))
@given(row=snapshots(), name=st.sampled_from(sorted(ParentSnapshot.model_fields)))
def test_a_snapshot_cannot_be_changed_once_built(row: ParentSnapshot, name: str) -> None:
    with pytest.raises(ValidationError, match="frozen"):
        setattr(row, name, getattr(row, name))
