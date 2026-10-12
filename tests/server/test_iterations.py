"""Bounded iteration query behavior over the public run view."""

import base64
import json

import pytest
from pydantic import ValidationError

from server.api.iterations import IterationQueryError, page_iterations
from server.api.protocol import IterationQuery
from vibesys.api import (
    IterationKind,
    IterationStatus,
    IterationSummary,
    IterationTree,
    IterationUnavailable,
    IterationUnavailableReason,
    RunStatus,
    RunView,
)


def _view(count: int = 5) -> RunView:
    return RunView(
        run_id="run-1",
        loop="evolve",
        status=RunStatus.ACTIVE,
        iterations=IterationTree.from_nodes(
            tuple(
                IterationSummary(
                    id=f"generation:{number}",
                    kind=IterationKind.GENERATION,
                    label=f"Generation {number}",
                    ordinal=number,
                    status=IterationStatus.COMPLETED,
                )
                for number in range(count)
            )
        ),
    )


def test_pages_are_bounded_and_cursor_continuations_are_exact() -> None:
    cursor = None
    pages = []
    while True:
        page = page_iterations(_view(), parent_id=None, cursor=cursor, limit=2)
        assert page.kind == "page"
        assert page.from_cursor == cursor
        assert len(page.items) <= 2
        pages.extend(item.id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert pages == [f"generation:{number}" for number in range(5)]


def test_pages_direct_children_without_recursing_into_their_parent() -> None:
    root_id = "generation:1"
    view = RunView(
        run_id="run-1",
        loop="evolve",
        status=RunStatus.ACTIVE,
        iterations=IterationTree.from_nodes(
            (
                IterationSummary(
                    id=root_id,
                    kind=IterationKind.GENERATION,
                    label="Generation 1",
                    ordinal=1,
                    status=IterationStatus.ACTIVE,
                    child_count=3,
                ),
                *(
                    IterationSummary(
                        id=f"candidate:{number}",
                        parent_id=root_id,
                        kind=IterationKind.CANDIDATE,
                        label=f"Candidate {number}",
                        ordinal=number,
                        status=IterationStatus.COMPLETED,
                    )
                    for number in range(3)
                ),
            )
        ),
    )

    first = page_iterations(view, parent_id=root_id, cursor=None, limit=2)
    assert first.kind == "page"
    assert [item.id for item in first.items] == ["candidate:0", "candidate:1"]
    assert first.next_cursor is not None

    second = page_iterations(view, parent_id=root_id, cursor=first.next_cursor, limit=2)
    assert second.kind == "page"
    assert [item.id for item in second.items] == ["candidate:2"]
    assert second.next_cursor is None


def test_cursor_free_page_is_an_authoritative_reset_at_the_current_revision() -> None:
    stale = page_iterations(_view(), parent_id=None, cursor=None, limit=2)
    reset = page_iterations(_view(6), parent_id=None, cursor=None, limit=2)

    assert stale.kind == "page"
    assert reset.kind == "page"
    assert reset.from_cursor is None
    assert reset.projection_revision != stale.projection_revision
    assert [item.id for item in reset.items] == ["generation:0", "generation:1"]


def test_empty_and_unsupported_projections_remain_distinct() -> None:
    empty = page_iterations(_view(0), parent_id=None, cursor=None, limit=100)
    unsupported = page_iterations(
        RunView(
            run_id="run-1",
            loop="plain",
            status=RunStatus.ACTIVE,
            iterations=IterationUnavailable(
                reason=IterationUnavailableReason.EXTERNAL_STATE_UNAVAILABLE
            ),
        ),
        parent_id=None,
        cursor=None,
        limit=100,
    )

    assert empty.kind == "page"
    assert empty.items == ()
    assert empty.next_cursor is None
    assert unsupported.kind == "unavailable"
    assert unsupported.reason is IterationUnavailableReason.EXTERNAL_STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("parent_id", "cursor", "message"),
    [
        ("missing", None, "parent_id"),
        (None, "not-a-cursor", "cursor"),
    ],
)
def test_invalid_query_names_the_offending_field(
    parent_id: str | None, cursor: str | None, message: str
) -> None:
    with pytest.raises(IterationQueryError, match=message):
        page_iterations(_view(), parent_id=parent_id, cursor=cursor, limit=2)


def test_cursor_is_rejected_after_projection_changes() -> None:
    first = page_iterations(_view(), parent_id=None, cursor=None, limit=2)
    assert first.kind == "page"
    assert first.next_cursor is not None

    with pytest.raises(IterationQueryError, match="stale"):
        page_iterations(_view(6), parent_id=None, cursor=first.next_cursor, limit=2)


def test_cursor_is_rejected_for_a_different_parent() -> None:
    first = page_iterations(_view(), parent_id=None, cursor=None, limit=2)
    assert first.kind == "page"
    assert first.next_cursor is not None

    with pytest.raises(IterationQueryError, match="parent_id"):
        page_iterations(_view(), parent_id="generation:0", cursor=first.next_cursor, limit=2)


def test_decodable_cursor_offset_that_server_could_not_emit_is_rejected() -> None:
    view = _view()
    assert isinstance(view.iterations, IterationTree)
    raw = json.dumps(
        {
            "version": 1,
            "revision": view.iterations.revision,
            "parent_id": None,
            "offset": len(view.iterations.nodes),
        },
        separators=(",", ":"),
    )
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    with pytest.raises(IterationQueryError, match=r"cursor\.offset"):
        page_iterations(view, parent_id=None, cursor=cursor, limit=2)


@pytest.mark.parametrize("limit", [0, 201])
def test_invalid_limits_fail_at_the_protocol_and_paging_boundaries(limit: int) -> None:
    with pytest.raises(ValidationError, match="limit"):
        IterationQuery.model_validate({"type": "query.iterations", "limit": limit})
    with pytest.raises(IterationQueryError, match="limit"):
        page_iterations(_view(), parent_id=None, cursor=None, limit=limit)


def test_cursor_is_rejected_for_an_unsupported_projection() -> None:
    view = RunView(
        run_id="run-1",
        loop="plain",
        status=RunStatus.ACTIVE,
        iterations=IterationUnavailable(
            reason=IterationUnavailableReason.EXTERNAL_STATE_UNAVAILABLE
        ),
    )

    with pytest.raises(IterationQueryError, match="cursor"):
        page_iterations(view, parent_id=None, cursor="opaque", limit=2)
