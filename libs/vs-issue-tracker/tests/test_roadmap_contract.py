"""Contract of the `Roadmap` interface, run against every implementation.

The same generated batches run against the in-memory fake and the document-backed roadmap. A
pure model (`roadmap_problem` and `merged` over the items so far) says what each batch must
leave, so no implementation is more or less forgiving than another. A later implementation
(a tracker's issues) joins `IMPLEMENTATIONS` and must pass the same file.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_issue_tracker.api import (
    DocumentRoadmap,
    FakeRoadmap,
    Roadmap,
    RoadmapItem,
    RoadmapItemKind,
    RoadmapLoadError,
    RoadmapRejectedError,
    RoadmapStatus,
    merged,
    roadmap_problem,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable


class MemoryDocuments:
    """A document store that keeps bytes by name and can be told to fail its next write."""

    def __init__(self) -> None:
        self.documents: dict[str, bytes] = {}
        self.fail_next_write = False

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        return self.documents.get(str(PurePosixPath(relative_path)))

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            message = "the store is unavailable"
            raise OSError(message)
        self.documents[str(PurePosixPath(relative_path))] = contents


IMPLEMENTATIONS: dict[str, Callable[[], Roadmap]] = {
    "fake": FakeRoadmap,
    "document": lambda: DocumentRoadmap(MemoryDocuments()),
}


EACH = pytest.mark.parametrize("implementation", sorted(IMPLEMENTATIONS))


_IDS = st.sampled_from(("major-1", "major-2", "minor-1", "minor-2", "x"))


@st.composite
def _items(draw: st.DrawFn) -> RoadmapItem:
    kind = draw(st.sampled_from(RoadmapItemKind))
    return RoadmapItem(
        id=draw(_IDS),
        kind=kind,
        title=draw(st.text(alphabet="abc ", min_size=1, max_size=8)),
        status=draw(st.sampled_from(RoadmapStatus)),
        major=draw(_IDS) if kind is RoadmapItemKind.MINOR else None,
        note=draw(st.text(alphabet="xyz", max_size=4)),
    )


_BATCHES = st.lists(st.lists(_items(), min_size=1, max_size=4), max_size=6)


def _major(item_id: str, status: RoadmapStatus = RoadmapStatus.TODO) -> RoadmapItem:
    return RoadmapItem(id=item_id, kind=RoadmapItemKind.MAJOR, title=item_id, status=status)


def _minor(item_id: str, major: str) -> RoadmapItem:
    return RoadmapItem(
        id=item_id,
        kind=RoadmapItemKind.MINOR,
        title=item_id,
        status=RoadmapStatus.TODO,
        major=major,
    )


@EACH
@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
@given(batches=_BATCHES)
def test_every_batch_leaves_what_the_model_says_or_is_refused_without_a_change(
    implementation: str, batches: list[list[RoadmapItem]]
) -> None:
    roadmap = IMPLEMENTATIONS[implementation]()
    model: tuple[RoadmapItem, ...] = ()
    for batch in batches:
        problem = roadmap_problem(model, batch)
        if problem is None:
            roadmap.put(batch)
            model = merged(model, batch)
        else:
            with pytest.raises(RoadmapRejectedError) as refused:
                roadmap.put(batch)
            assert refused.value.problem == problem
        assert roadmap.items() == model


@EACH
@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
@given(batches=_BATCHES)
def test_putting_an_accepted_batch_again_changes_nothing(
    implementation: str, batches: list[list[RoadmapItem]]
) -> None:
    roadmap = IMPLEMENTATIONS[implementation]()
    for batch in batches:
        try:
            roadmap.put(batch)
        except RoadmapRejectedError:
            continue
        before = roadmap.items()
        roadmap.put(batch)
        assert roadmap.items() == before


@EACH
def test_an_update_replaces_an_item_in_place_and_a_new_id_is_appended(
    implementation: str,
) -> None:
    roadmap = IMPLEMENTATIONS[implementation]()
    roadmap.put([_major("major-1"), _major("major-2")])
    roadmap.put([_major("major-3"), _major("major-1", RoadmapStatus.PARKED)])

    assert [(item.id, item.status) for item in roadmap.items()] == [
        ("major-1", RoadmapStatus.PARKED),
        ("major-2", RoadmapStatus.TODO),
        ("major-3", RoadmapStatus.TODO),
    ]


@EACH
def test_a_minor_may_name_a_major_put_in_the_same_batch_but_not_an_absent_one(
    implementation: str,
) -> None:
    roadmap = IMPLEMENTATIONS[implementation]()
    roadmap.put([_minor("minor-1", "major-1"), _major("major-1")])

    with pytest.raises(RoadmapRejectedError, match="minor 'minor-2' names 'major-9'"):
        roadmap.put([_major("major-2"), _minor("minor-2", "major-9")])
    assert [item.id for item in roadmap.items()] == ["minor-1", "major-1"]


@EACH
def test_an_update_cannot_change_an_items_kind(implementation: str) -> None:
    roadmap = IMPLEMENTATIONS[implementation]()
    roadmap.put([_major("major-1"), _major("item")])

    with pytest.raises(RoadmapRejectedError, match="'item' is a major"):
        roadmap.put([_minor("item", "major-1")])


def test_a_minor_must_name_its_major_and_a_major_must_not() -> None:
    with pytest.raises(ValueError, match="must name the Major it blocks"):
        RoadmapItem.model_validate_json(
            '{"id": "minor-1", "kind": "minor", "title": "t", "status": "todo"}'
        )
    with pytest.raises(ValueError, match="must not name a Major"):
        RoadmapItem.model_validate_json(
            '{"id": "major-1", "kind": "major", "title": "t", "status": "todo", "major": "major-2"}'
        )


def test_the_document_roadmap_is_read_back_by_a_new_client_of_the_same_store() -> None:
    documents = MemoryDocuments()
    DocumentRoadmap(documents).put([_major("major-1"), _minor("minor-1", "major-1")])

    assert [item.id for item in DocumentRoadmap(documents).items()] == ["major-1", "minor-1"]


def test_a_failed_document_write_leaves_the_earlier_items() -> None:
    documents = MemoryDocuments()
    roadmap = DocumentRoadmap(documents)
    roadmap.put([_major("major-1")])
    documents.fail_next_write = True

    with pytest.raises(OSError, match="unavailable"):
        roadmap.put([_major("major-2")])
    assert [item.id for item in roadmap.items()] == ["major-1"]


def test_a_corrupt_document_is_a_load_error_that_names_the_document() -> None:
    documents = MemoryDocuments()
    documents.documents["roadmap.json"] = b'{"version": 1, "items": [{"id": "BAD"}]}'

    with pytest.raises(RoadmapLoadError, match=r"roadmap\.json"):
        DocumentRoadmap(documents).items()
