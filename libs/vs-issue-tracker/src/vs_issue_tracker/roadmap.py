"""A search's roadmap: typed planning items behind a storage-neutral interface.

A designer plans across rounds with a short list of directions (Majors) and the blockers that
stand in their way (Minors), each with a status. `Roadmap` is where those items are kept. Its
callers see only typed items, never where or how they are stored: `DocumentRoadmap` keeps them
as one document in a store the application opens (VibeSys keeps it in the run's state), and a
tracker such as GitHub issues can implement the same interface later without changing a caller.

The rules an implementation enforces are the pure `roadmap_problem` and `merged`, so every
implementation, the Fake included, accepts and leaves exactly the same items.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import Field, model_validator

from vs_core.api import Value, markdown_prose, plain_line
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import PurePosixPath

ROADMAP_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}$"
"""A roadmap item's id: a short lowercase slug such as ``major-1``."""
TITLE_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 1000


class RoadmapItemKind(StrEnum):
    """What a roadmap item is: a direction, or a blocker of one."""

    MAJOR = "major"
    MINOR = "minor"


class RoadmapStatus(StrEnum):
    """Where a roadmap item stands."""

    TODO = "todo"
    """Not started."""
    IN_PROGRESS = "in_progress"
    """Being worked on now."""
    DONE = "done"
    """Proved complete."""
    PARKED = "parked"
    """A credible direction whose implementation was defective."""
    ABANDONED = "abandoned"
    """The mechanism cannot help this workload."""


class RoadmapItem(Value):
    """One roadmap item, identified by its ``id`` for as long as the roadmap is kept."""

    id: str = Field(
        pattern=ROADMAP_ID_PATTERN,
        description="Stable short slug naming the item, such as `major-1`; reuse it to update the item.",
    )
    kind: RoadmapItemKind = Field(
        description="`major` for a direction, `minor` for a blocker of one Major."
    )
    title: str = Field(
        min_length=1,
        max_length=TITLE_MAX_LENGTH,
        description=plain_line("What the item is, in a line."),
    )
    status: RoadmapStatus = Field(description="Where the item stands now.")
    major: str | None = Field(
        default=None,
        pattern=ROADMAP_ID_PATTERN,
        description="For a `minor`, the id of the Major it blocks; null for a `major`.",
    )
    note: str = Field(
        default="",
        max_length=NOTE_MAX_LENGTH,
        description=markdown_prose(
            "Optional short rationale, such as why the item was parked or abandoned."
        ),
    )

    @model_validator(mode="after")
    def _a_minor_names_its_major(self) -> Self:
        if self.kind is RoadmapItemKind.MINOR and self.major is None:
            message = f"minor {self.id!r} must name the Major it blocks"
            raise ValueError(message)
        if self.kind is RoadmapItemKind.MAJOR and self.major is not None:
            message = f"major {self.id!r} must not name a Major"
            raise ValueError(message)
        return self


class RoadmapRejectedError(ValueError):
    """A batch of items the roadmap refuses; nothing of it was stored."""

    def __init__(self, problem: str) -> None:
        """Name what is wrong with the batch."""
        super().__init__(problem)
        self.problem = problem


class Roadmap(Protocol):
    """Storage-neutral contract for a search's roadmap.

    Items are identified by id. ``put`` creates or replaces each item of a batch, atomically:
    a batch `roadmap_problem` refuses raises `RoadmapRejectedError` and changes nothing, and a
    storage failure leaves either the whole batch or none of it. Putting the same batch twice
    leaves the same items as putting it once. Items are never deleted; a direction that ends is
    marked done, parked or abandoned. Reads observe every batch put before them.
    """

    def items(self) -> tuple[RoadmapItem, ...]:
        """Every item, in the order each id was first put."""
        ...

    def put(self, items: Sequence[RoadmapItem]) -> None:
        """Create or replace each item by its id, as one batch."""
        ...


def roadmap_problem(current: Sequence[RoadmapItem], batch: Sequence[RoadmapItem]) -> str | None:
    """Why ``batch`` cannot be put over ``current``, or None when it can.

    A batch names each id once, never changes an existing item's kind, and leaves every
    Minor naming a Major the roadmap holds.
    """
    ids = [item.id for item in batch]
    repeated = sorted({item for item in ids if ids.count(item) > 1})
    if repeated:
        return f"the roadmap update names {', '.join(map(repr, repeated))} more than once"
    held = {item.id: item for item in current}
    for item in batch:
        before = held.get(item.id)
        if before is not None and before.kind is not item.kind:
            return (
                f"roadmap item {item.id!r} is a {before.kind.value}; "
                f"an update cannot make it a {item.kind.value}"
            )
    after = {item.id: item for item in merged(current, batch)}
    for item in batch:
        if item.major is not None and (
            item.major not in after or after[item.major].kind is not RoadmapItemKind.MAJOR
        ):
            return f"minor {item.id!r} names {item.major!r}, which is not a Major on the roadmap"
    return None


def merged(current: Sequence[RoadmapItem], batch: Sequence[RoadmapItem]) -> tuple[RoadmapItem, ...]:
    """``current`` with each item of ``batch`` replacing its id, new ids appended in order."""
    replacing = {item.id: item for item in batch}
    kept = tuple(replacing.pop(item.id, item) for item in current)
    return (*kept, *(item for item in batch if item.id in replacing))


def _checked(
    current: Sequence[RoadmapItem], batch: Sequence[RoadmapItem]
) -> tuple[RoadmapItem, ...]:
    problem = roadmap_problem(current, batch)
    if problem is not None:
        raise RoadmapRejectedError(problem)
    return merged(current, batch)


class FakeRoadmap:
    """An in-memory roadmap with the contract's semantics, for tests."""

    def __init__(self, items: Sequence[RoadmapItem] = ()) -> None:
        """Start holding ``items`` (which must form a valid roadmap)."""
        self._items = _checked((), items)

    def items(self) -> tuple[RoadmapItem, ...]:
        """Every item, in the order each id was first put."""
        return self._items

    def put(self, items: Sequence[RoadmapItem]) -> None:
        """Create or replace each item by its id, as one batch."""
        self._items = _checked(self._items, items)


class RoadmapDocuments(Protocol):
    """Where `DocumentRoadmap` keeps its one document: a store of named byte documents."""

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        """One document's bytes, or None when it is absent."""
        ...

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Replace one document atomically."""
        ...


ROADMAP_DOCUMENT = "roadmap.json"
"""The name of the document `DocumentRoadmap` keeps its items in."""


_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
"""The largest stored roadmap document read back."""


class _RoadmapDocument(Value):
    """The stored form of a roadmap: a version and every item."""

    version: Literal[1] = 1
    items: tuple[RoadmapItem, ...] = ()


class RoadmapLoadError(ValueError):
    """The stored roadmap document cannot be read as a roadmap."""


class DocumentRoadmap:
    """A roadmap kept as one JSON document in a document store, replaced whole on every put.

    The store's atomic replace makes each batch all or nothing. The document is read on every
    call, so another client's put is seen by the next read.
    """

    def __init__(self, documents: RoadmapDocuments) -> None:
        """Bind the store the document lives in."""
        self._documents = documents

    def items(self) -> tuple[RoadmapItem, ...]:
        """Every item, in the order each id was first put."""
        stored = self._documents.read_bytes(ROADMAP_DOCUMENT)
        if stored is None:
            return ()
        decoded = decode_json(
            stored, _RoadmapDocument, source=ROADMAP_DOCUMENT, max_bytes=_MAX_DOCUMENT_BYTES
        )
        if isinstance(decoded, Unreadable):
            message = f"the stored roadmap is not a roadmap: {decoded}"
            raise RoadmapLoadError(message)
        return decoded.items

    def put(self, items: Sequence[RoadmapItem]) -> None:
        """Create or replace each item by its id, as one batch."""
        document = _RoadmapDocument(items=_checked(self.items(), items))
        self._documents.write_bytes(ROADMAP_DOCUMENT, document.model_dump_json(indent=2).encode())


__all__ = [
    "NOTE_MAX_LENGTH",
    "ROADMAP_DOCUMENT",
    "ROADMAP_ID_PATTERN",
    "TITLE_MAX_LENGTH",
    "DocumentRoadmap",
    "FakeRoadmap",
    "Roadmap",
    "RoadmapDocuments",
    "RoadmapItem",
    "RoadmapItemKind",
    "RoadmapLoadError",
    "RoadmapRejectedError",
    "RoadmapStatus",
    "merged",
    "roadmap_problem",
]
