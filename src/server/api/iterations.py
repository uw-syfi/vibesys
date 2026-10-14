"""Bounded paging over the authoritative VibeSys iteration projection."""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import IterationSummary, IterationTree, IterationUnavailable, RunView
from vs_decode.api import Unreadable, ValidationOptions, decode_json

MAX_ITERATION_PAGE_SIZE = 200


class IterationQueryError(ValueError):
    """A cursor or parent that cannot address the current projection."""

    diagnostic_code = "iteration_query_invalid"

    @classmethod
    def unavailable_cursor(cls) -> IterationQueryError:
        """Reject a cursor for a policy without iteration navigation."""
        return cls("cursor: iterations are unavailable for this policy")

    @classmethod
    def offset_out_of_range(cls, offset: int, count: int) -> IterationQueryError:
        """Reject an offset beyond the current sibling set."""
        return cls(f"cursor.offset: {offset} is outside child count {count}")

    @classmethod
    def unknown_parent(cls, parent_id: str) -> IterationQueryError:
        """Reject a parent absent from the projection."""
        return cls(f"parent_id: unknown iteration {parent_id!r}")

    @classmethod
    def malformed_cursor(cls) -> IterationQueryError:
        """Reject a cursor that cannot be decoded strictly."""
        return cls("cursor: malformed iteration cursor")

    @classmethod
    def invalid_limit(cls, limit: int) -> IterationQueryError:
        """Reject page sizes outside the public protocol bound."""
        return cls(f"limit: {limit} is outside the allowed range 1..{MAX_ITERATION_PAGE_SIZE}")

    @classmethod
    def stale_cursor(cls) -> IterationQueryError:
        """Reject a cursor for a prior projection revision."""
        return cls("cursor: projection revision is stale")

    @classmethod
    def wrong_parent(cls) -> IterationQueryError:
        """Reject a cursor used for a different parent query."""
        return cls("cursor: parent_id does not match the query")


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    revision: str
    parent_id: str | None
    offset: int = Field(gt=0)


class IterationSlice(BaseModel):
    """One bounded page, with enough identity to reject stale merges."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["page"] = "page"
    schema_version: Literal[1] = 1
    run_id: str
    projection_revision: str
    parent_id: str | None
    from_cursor: str | None = None
    items: tuple[IterationSummary, ...]
    next_cursor: str | None = None


def page_iterations(
    view: RunView,
    *,
    parent_id: str | None,
    cursor: str | None,
    limit: int,
) -> IterationSlice | IterationUnavailable:
    """Return one children page or the policy's explicit unavailable result."""
    if limit < 1 or limit > MAX_ITERATION_PAGE_SIZE:
        raise IterationQueryError.invalid_limit(limit)
    projection = view.iterations
    if isinstance(projection, IterationUnavailable):
        if cursor is not None:
            raise IterationQueryError.unavailable_cursor()
        return projection
    _require_parent(projection, parent_id)
    offset = _decode_cursor(cursor, projection, parent_id) if cursor is not None else 0
    children = tuple(
        sorted(
            (node for node in projection.nodes if node.parent_id == parent_id),
            key=lambda node: (node.ordinal, node.id),
        )
    )
    if cursor is not None and offset >= len(children):
        raise IterationQueryError.offset_out_of_range(offset, len(children))
    items = children[offset : offset + limit]
    next_offset = offset + len(items)
    next_cursor = (
        _encode_cursor(projection.revision, parent_id, next_offset)
        if next_offset < len(children)
        else None
    )
    return IterationSlice(
        run_id=view.run_id,
        projection_revision=projection.revision,
        parent_id=parent_id,
        from_cursor=cursor,
        items=items,
        next_cursor=next_cursor,
    )


def _require_parent(projection: IterationTree, parent_id: str | None) -> None:
    if parent_id is None:
        return
    if not any(node.id == parent_id for node in projection.nodes):
        raise IterationQueryError.unknown_parent(parent_id)


_MAX_CURSOR_BYTES = 4096
"""Far past any cursor this server issues; a longer one is malformed."""


def _encode_cursor(revision: str, parent_id: str | None, offset: int) -> str:
    raw = _Cursor(revision=revision, parent_id=parent_id, offset=offset).model_dump_json()
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, projection: IterationTree, parent_id: str | None) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError as error:
        raise IterationQueryError.malformed_cursor() from error
    decoded = decode_json(
        raw,
        _Cursor,
        source="cursor",
        max_bytes=_MAX_CURSOR_BYTES,
        options=ValidationOptions(strict=True),
    )
    if isinstance(decoded, Unreadable):
        raise IterationQueryError.malformed_cursor()
    if decoded.revision != projection.revision:
        raise IterationQueryError.stale_cursor()
    if decoded.parent_id != parent_id:
        raise IterationQueryError.wrong_parent()
    return decoded.offset


__all__ = [
    "MAX_ITERATION_PAGE_SIZE",
    "IterationQueryError",
    "IterationSlice",
    "page_iterations",
]
