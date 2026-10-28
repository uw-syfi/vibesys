"""A settled attempt's changed files on its workstream span, with its diff attached.

The run reads the changes from its repository when the attempt settles (``vibesys.run``); this
projection only places them: the diff as a blob attached under role ``diff``, and every file as a
``FileChange`` naming its byte range in that blob (empty when the diff was cut before it, or git
quoted its path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import vs_proto.api as p
from vibesys.events import CoreEventType, make_core_event
from vibesys.records._common import Projection

if TYPE_CHECKING:
    from datetime import datetime

    from vibesys.records._types import MappedEvent, MappingState
    from vibesys.workspace_changes import AttemptChanges

_HEADER = b"diff --git "

FILES_BYTES = 512 << 10
"""Encoded bytes of file entries one span record holds, half the record cap: every file of any
attempt a person reads fits.

ponytail: an attempt past this (tens of thousands of files) lists the files that fit; the diff still
holds them all up to its own cap. A ``files`` blob would lift it.
"""


def settled_attempts(records: tuple[p.Record, ...]) -> list[tuple[str, str, str]]:
    """Each workstream span these records end that names its range and lists no files yet.

    Returned as ``(span, parent_revision, revision)``, for the run to read what changed.
    """
    found: list[tuple[str, str, str]] = []
    for record in records:
        body = record.body
        if body is None or body.field != "span" or body.value.span is None:
            continue
        span = body.value.span
        label = span.label
        if (
            not span.has_field("ended_at")
            or label is None
            or label.kind != "workstream"
            or label.attrs is None
            or not label.attrs.type_url.endswith("/" + p.WorkstreamAttrs().desc().type_name)
        ):
            continue
        attrs = p.WorkstreamAttrs.from_binary(label.attrs.value)
        if attrs.files or not attrs.has_field("revision") or not attrs.has_field("parent_revision"):
            continue
        found.append((span.id, attrs.parent_revision, attrs.revision))
    return found


def _sections(diff: bytes) -> dict[bytes, tuple[int, int]]:
    """Each file section of a unified diff by its header line, as a byte range."""
    starts = [0] if diff.startswith(_HEADER) else []
    at = diff.find(b"\n" + _HEADER)
    while at >= 0:
        starts.append(at + 1)
        at = diff.find(b"\n" + _HEADER, at + 1)
    sections: dict[bytes, tuple[int, int]] = {}
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(diff)
        newline = diff.find(b"\n", start, end)
        header = diff[start : newline if newline >= 0 else end]
        sections[header] = (start, end - start)
    return sections


def attach_changes(
    state: MappingState, span_id: str, changes: AttemptChanges, at: datetime
) -> MappedEvent:
    """Write ``changes`` onto the ended workstream ``span_id``, once; nothing when it has files."""
    projection = Projection(state, make_core_event(CoreEventType.RUN_CONFIGURED, timestamp=at))
    span = projection.spans.get(span_id)
    label = None if span is None else span.label
    if span is None or label is None or label.kind != "workstream" or label.attrs is None:
        return projection.result()
    attrs = p.WorkstreamAttrs.from_binary(label.attrs.value)
    if attrs.files or not changes.files:
        return projection.result()
    diff = changes.diff.encode()
    if diff:
        blob = projection.blob(diff, "text/x-diff; charset=utf-8")
        projection.emit("blob", p.BlobAttached(span=span_id, role="diff", blob=blob))
    sections = _sections(diff)
    room = FILES_BYTES
    for change in changes.files:
        source = change.renamed_from or change.path
        offset, length = sections.get(f"diff --git a/{source} b/{change.path}".encode(), (0, 0))
        entry = p.FileChange(path=change.path, diff_offset=offset, diff_length=length)
        room -= len(entry.to_binary()) + 4
        if room < 0:
            break
        attrs.files.append(entry)
    label.attrs = projection.pack(attrs, span_id)
    projection.span(span, p.Change.UPDATED)
    return projection.result()
