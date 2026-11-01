"""ReceiptStore: crash-safe keyed documents, under one cross-process lock."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from vs_core.api import ContractError
from vs_project.api import Project, ProjectStateError
from vs_runtime.api.core import ReceiptCorruptError, ReceiptStore
from vs_sim.api.testing import SimThreads, examples

FAMILY = "documents"
PART = "doc"


class Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: int


def store_at(root: Path) -> ReceiptStore:
    (root / "project").mkdir(exist_ok=True)
    return ReceiptStore(
        Project.open(root / "project").state.state_store_namespace("run"), threads=SimThreads()
    )


KEYS = st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12)
RELAXED = settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=examples(pr=8, nightly=40),
)


@RELAXED
@given(key=KEYS, value=st.integers(0, 10**6), repeats=st.integers(1, 4))
def test_recording_the_same_document_again_changes_nothing(
    key: str, value: int, repeats: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        for _ in range(repeats):
            store.record_once(FAMILY, PART, key, Doc(value=value))
        assert store.load(FAMILY, PART, key, Doc) == Doc(value=value)


@RELAXED
@given(key=KEYS, first=st.integers(0, 10**6), other=st.integers(0, 10**6))
def test_another_document_under_a_recorded_key_conflicts_and_the_first_survives(
    key: str, first: int, other: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        store.record_once(FAMILY, PART, key, Doc(value=first))
        if other == first:
            store.record_once(FAMILY, PART, key, Doc(value=other))
        else:
            with pytest.raises(ContractError):
                store.record_once(FAMILY, PART, key, Doc(value=other))
        assert store.load(FAMILY, PART, key, Doc) == Doc(value=first)


@RELAXED
@given(key=KEYS, values=st.lists(st.integers(0, 100), min_size=1, max_size=6))
def test_modify_sees_the_stored_document_and_stores_what_it_decides(
    key: str, values: list[int]
) -> None:
    """Each modification reads the document the one before wrote, and may decide to keep it."""
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        seen: list[int | None] = []
        for value in values:

            def decide(stored: Doc | None, value: int = value) -> tuple[Doc | None, int]:
                seen.append(None if stored is None else stored.value)
                return Doc(value=value), value

            assert store.modify(FAMILY, PART, key, Doc, decide) == value
        assert seen == [None, *values[:-1]]
        assert store.load(FAMILY, PART, key, Doc) == Doc(value=values[-1])
        assert store.modify(FAMILY, PART, key, Doc, lambda _stored: (None, 0)) == 0
        assert store.load(FAMILY, PART, key, Doc) == Doc(value=values[-1])


@RELAXED
@given(key=KEYS, values=st.lists(st.integers(0, 100), min_size=1, max_size=4))
def test_replace_overwrites_a_document_defined_to_change(key: str, values: list[int]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        for value in values:
            store.replace(FAMILY, PART, key, Doc(value=value))
        assert store.load(FAMILY, PART, key, Doc) == Doc(value=values[-1])


def test_a_key_nothing_recorded_loads_as_none() -> None:
    with tempfile.TemporaryDirectory() as raw:
        assert store_at(Path(raw)).load(FAMILY, PART, "nothing", Doc) is None


def test_exclusion_can_be_taken_again_by_the_holder() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        with store.exclusive(), store.exclusive(blocking=False):
            store.replace(FAMILY, PART, "k", Doc(value=1))
        assert store.load(FAMILY, PART, "k", Doc) == Doc(value=1)


def test_a_damaged_receipt_is_corruption_and_an_invalid_path_is_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        store.replace(FAMILY, PART, "k", Doc(value=1))
        for path in (Path(raw) / "project").rglob(f"*.{PART}.json"):
            path.write_text("secret-receipt-bytes")
        with pytest.raises(ReceiptCorruptError) as damaged:
            store.load(FAMILY, PART, "k", Doc)
        assert "secret-receipt-bytes" not in str(damaged.value.__cause__)
        # A family that is not a safe path is a caller bug, not a damaged receipt.
        with pytest.raises(ProjectStateError) as invalid:
            store.load("../escape", PART, "k", Doc)
        assert not isinstance(invalid.value, ReceiptCorruptError)
