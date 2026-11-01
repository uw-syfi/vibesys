"""A submission owed to an agent is found by its full identity: scope, revision and policy.

The record is how a retry after a lost reply gets the same handle and charges nothing, so a
lookup that matched on less would hand an agent another measurement's handle (another policy is
another measurement), and a clear that matched on less would forget an entry still owed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import AttemptId, RunId, Scope
from vs_project.api import Project
from vs_runtime.api.core import KeyedDocuments, OwedKey, OwedSubmission, OwedSubmissions
from vs_sim.api.testing import SimThreads

type Parts = tuple[Scope, str, str]

SCOPES = (
    Scope(owner=AttemptId(root="a1"), generation=0),
    Scope(owner=AttemptId(root="a1"), generation=1),
    Scope(owner=AttemptId(root="a2"), generation=0),
    Scope(owner=RunId(root="run"), generation=0),
)
KEYS = st.tuples(
    st.sampled_from(SCOPES), st.sampled_from(("r1", "r2")), st.sampled_from(("p1", "p2"))
)


def owed_in(root: Path) -> OwedSubmissions:
    namespace = Project.open(root).state.state_store_namespace("owed")
    return OwedSubmissions(KeyedDocuments(namespace, threads=SimThreads()))


def key_of(parts: Parts) -> OwedKey:
    scope, revision, policy = parts
    return OwedKey(scope=scope, revision=revision, policy=policy)


def call_of(owed: OwedSubmissions, parts: Parts) -> str | None:
    found = owed.find(key_of(parts))
    return None if found is None else found.call_id


def entry(parts: Parts, call_id: str) -> OwedSubmission:
    return OwedSubmission(key=key_of(parts), call_id=call_id, submitted_at=1.0)


@given(recorded=st.lists(KEYS, unique=True, max_size=6), asked=KEYS)
def test_an_entry_is_found_by_exactly_the_key_it_was_recorded_under(
    recorded: list[Parts], asked: Parts
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        owed = owed_in(Path(raw))
        for index, parts in enumerate(recorded):
            owed.record(entry(parts, f"call-{index}"))

        found = owed.find(key_of(asked))

        assert (found is not None) == (asked in recorded)
        if found is not None:
            assert found.call_id == f"call-{recorded.index(asked)}"
        assert len(owed.all()) == len(recorded)


@given(recorded=st.lists(KEYS, unique=True, min_size=1, max_size=6), data=st.data())
def test_a_clear_drops_only_the_entry_of_its_key_and_call(
    recorded: list[Parts], data: st.DataObject
) -> None:
    target = data.draw(st.sampled_from(recorded))
    wrong_call = data.draw(st.booleans())
    with tempfile.TemporaryDirectory() as raw:
        owed = owed_in(Path(raw))
        for index, parts in enumerate(recorded):
            owed.record(entry(parts, f"call-{index}"))
        call = f"call-{recorded.index(target)}"

        owed.clear(key_of(target), "another-call" if wrong_call else call)

        left = {item.call_id for item in owed.all()}
        everything = {f"call-{index}" for index in range(len(recorded))}
        assert left == (everything if wrong_call else everything - {call})


def test_recording_again_under_one_key_replaces_it_and_only_it() -> None:
    first, other = (SCOPES[0], "r1", "p1"), (SCOPES[0], "r1", "p2")
    with tempfile.TemporaryDirectory() as raw:
        owed = owed_in(Path(raw))
        owed.record(entry(first, "old"))
        owed.record(entry(other, "other"))
        owed.record(entry(first, "new"))

        assert call_of(owed, first) == "new"
        assert call_of(owed, other) == "other"
        assert len(owed.all()) == 2


def test_the_record_outlives_the_object_that_wrote_it() -> None:
    parts = (SCOPES[2], "r2", "p1")
    with tempfile.TemporaryDirectory() as raw:
        owed_in(Path(raw)).record(entry(parts, "kept"))

        assert call_of(owed_in(Path(raw)), parts) == "kept"
