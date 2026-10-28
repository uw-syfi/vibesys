"""A settled attempt's changed files on its workstream span, each naming its part of the diff."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.records.api import MappingState, attach_changes, map_event, settled_attempts
from vibesys.workspace_changes import AttemptChanges, WorkspaceChange, WorkspaceChangeKind

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_BASE = "a" * 40
_HEAD = "b" * 40

_PATHS = st.lists(
    st.text("abcdef/._-", min_size=1, max_size=12).filter(lambda path: path.strip("/") == path),
    min_size=1,
    max_size=8,
    unique=True,
)


def _workstream(attrs: dict[str, object], *, ended: bool) -> e.CoreEvent:
    span = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": "op:work",
                "parent": None,
                "type": "operation",
                "name": "attempt",
                "status": "done" if ended else "running",
                "started_at": 0,
                "ended_at": 3 if ended else None,
                "label": {"kind": "workstream", "attrs_json": json.dumps(attrs)},
            }
        }
    ).span
    return e.CoreEvent.model_validate(
        {
            "type": e.CoreEventType.SPAN_ENDED if ended else e.CoreEventType.SPAN_STARTED,
            "data": e.SpanEndedData(span=span) if ended else e.SpanStartedData(span=span),
            "timestamp": _AT,
            "run_id": "run-a",
            "sequence": 1,
        }
    )


def _diff(paths: list[str], cut: int) -> str:
    """A unified diff of ``paths``, the ones from ``cut`` on left out as a capped diff leaves them."""
    return "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new {index}\n"
        for index, path in enumerate(paths[:cut])
    )


def _attrs(records: tuple[p.Record, ...]) -> p.WorkstreamAttrs:
    span = next(
        record.body.value.span
        for record in reversed(records)
        if record.body is not None and record.body.field == "span" and record.body.value.span
    )
    assert span.label is not None
    assert span.label.attrs is not None
    return p.WorkstreamAttrs.from_binary(span.label.attrs.value)


@given(paths=_PATHS, data=st.data())
def test_every_file_names_its_own_part_of_the_attached_diff(
    paths: list[str], data: st.DataObject
) -> None:
    cut = data.draw(st.integers(min_value=0, max_value=len(paths)))
    diff = _diff(paths, cut)
    changes = AttemptChanges(
        files=tuple(
            WorkspaceChange(path=path, kind=WorkspaceChangeKind.MODIFIED) for path in paths
        ),
        diff=diff,
    )
    ended = map_event(
        MappingState("run-a"),
        _workstream({"parent_revision": _BASE, "revision": _HEAD}, ended=True),
    )
    assert settled_attempts(ended.records) == [("op:work", _BASE, _HEAD)]

    placed = attach_changes(ended.state, "op:work", changes, _AT)

    for record in placed.records:
        p.validate(record)
    files = _attrs(placed.records).files
    assert [file.path for file in files] == paths
    attached = [
        record.body.value
        for record in placed.records
        if record.body is not None and record.body.field == "blob"
    ]
    blob = b"".join(write.content for write in placed.blobs)
    assert [(each.span, each.role) for each in attached] == ([("op:work", "diff")] if diff else [])
    assert blob == diff.encode()
    for index, file in enumerate(files):
        part = blob[file.diff_offset : file.diff_offset + file.diff_length]
        if index < cut:
            assert part.startswith(f"diff --git a/{file.path} b/{file.path}\n".encode())
            assert part.endswith(f"+new {index}\n".encode())
        else:
            assert (file.diff_offset, file.diff_length) == (0, 0)
    # Once listed, an attempt is never read or written again.
    assert settled_attempts(placed.records) == []
    assert attach_changes(placed.state, "op:work", changes, _AT).records == ()


@pytest.mark.parametrize(
    ("attrs", "ended"),
    [
        ({"parent_revision": _BASE, "revision": _HEAD}, False),
        ({"parent_revision": _BASE}, True),
        ({"revision": _HEAD}, True),
        ({}, True),
    ],
)
def test_an_attempt_that_kept_nothing_or_still_works_is_not_read(
    attrs: dict[str, object], *, ended: bool
) -> None:
    result = map_event(MappingState("run-a"), _workstream(attrs, ended=ended))
    assert settled_attempts(result.records) == []
