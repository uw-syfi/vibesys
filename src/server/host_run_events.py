"""The host helper's reads of a run's recorded events: from a sequence, of a span, the newest.

``runs.read_events`` pages a run's journal from a sequence. ``runs.events_for_span`` and
``runs.read_tail`` skip to part of it through the journal's span index (``server.span_index``):
the events of one span and the spans beneath it, and the newest events before a sequence. All
three return events as JSON documents the client narrows with the run protocol's ``RunEvent``.

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
these messages into the helper's request and reply unions and gates each behind its own
``HelperFeature``.

``runs.read_events`` can also select events by kind (``RunEventKinds``): the trace's own events,
which is all a client needs to place an ended run's scrubber and which are a small part of a
journal. ``EVENT_TYPES_BY_KINDS`` is the one definition of which event types each kind selects;
the generated host-helper contract exports it so a client never keeps a second list.

Public surface: ``RUN_EVENTS_READ_MAX``, ``RUN_EVENTS_LEGACY_READ_MAX``, ``RunEventKinds``,
``EVENT_TYPES_BY_KINDS``, ``RunEventDocument``, ``RunsReadEventsRequest``, ``RunsReadEventsReply``, ``RunsEventsForSpanRequest``,
``RunsEventsForSpanReply``, ``RunsReadTailRequest`` and ``RunsReadTailReply``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import Field, JsonValue, NonNegativeInt, PositiveInt

from server.host_envelope import HelperReplyBase, HelperRequestBase, RunId, SpanId

if TYPE_CHECKING:
    from collections.abc import Mapping

RUN_EVENTS_READ_MAX: Final = 2000
"""The most events one ``runs.read_events`` may ask for; more than the legacy bound needs ``run_history_pages``."""
RUN_EVENTS_LEGACY_READ_MAX: Final = 500
"""The most events a helper without ``run_history_pages`` serves in one page."""


class RunEventKinds(StrEnum):
    """A closed set of event types ``runs.read_events`` can select (``EVENT_TYPES_BY_KINDS``)."""

    TRACE = "trace"
    """The events a client folds into the run trace: the spans' lifecycle, the run's declaration
    and the gates that finished."""


EVENT_TYPES_BY_KINDS: Final[Mapping[RunEventKinds, tuple[str, ...]]] = {
    RunEventKinds.TRACE: (
        "span_started",
        "span_updated",
        "span_ended",
        "run_started",
        "gate_finished",
    ),
}
"""The event ``type`` of each event a kind selects, in the generated contract as
``x-vibesys-event-types-by-kinds``. A kind selects an event by its top-level ``type``."""

RunEventDocument = dict[str, JsonValue]
"""One recorded run event as JSON: the run protocol's ``RunEvent``, which a client narrows
with its generated type. The helper passes it through so it never imports the protocol."""


class RunsReadEventsRequest(HelperRequestBase):
    """A page of one run's recorded events, from its journal on disk, live or ended."""

    type: Literal["runs.read_events"] = "runs.read_events"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_id: RunId
    from_sequence: PositiveInt
    """The first sequence to return; a run's first event is sequence 1."""
    max: Annotated[int, Field(ge=1, le=RUN_EVENTS_READ_MAX)]
    with_totals: bool | None = None
    """Ask for ``total_events`` and ``total_bytes``. Sent only after ``run_history_pages``."""
    kinds: RunEventKinds | None = None
    """Return only the events of this kind (``EVENT_TYPES_BY_KINDS``), in sequence order from
    ``from_sequence``, under the same ``max`` and byte bound. A page that is not ``complete``
    holds at least one event; ``next_sequence`` is the sequence after the last event the page
    read, so a stretch of other events costs a client no request. With ``with_totals`` the totals
    count the selected events only. Sent only after ``run_trace_events``."""


class RunsEventsForSpanRequest(HelperRequestBase):
    """A page of the recorded events of one run-trace span, from its journal on disk.

    The events of ``span_id`` and of every span beneath it, oldest first: an event belongs
    to the span it names in ``span_id`` and, when it names none, to the span whose
    ``invocation`` is its ``execution_id``. For an agent turn that is the turn's output, tool
    calls and results, activity and usage; for a flow, those of every turn under it. Span
    events themselves are not included: the trace already carries them.
    """

    type: Literal["runs.events_for_span"] = "runs.events_for_span"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_id: RunId
    span_id: SpanId = Field(title="Requested Span Id")
    from_sequence: PositiveInt = 1
    """The first sequence to return: the reply's ``next_sequence`` for the next page."""
    max: Annotated[int, Field(ge=1, le=RUN_EVENTS_READ_MAX)]


class RunsReadTailRequest(HelperRequestBase):
    """The newest recorded events of one run before a sequence, from its journal on disk."""

    type: Literal["runs.read_tail"] = "runs.read_tail"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_id: RunId
    before_sequence: PositiveInt | None = None
    """Only events with a lower sequence: the reply's first event's for the next older page.
    ``None`` reads from the journal's end."""
    tail: Annotated[int, Field(ge=1, le=RUN_EVENTS_READ_MAX)]
    """The most events to return."""


class RunsReadEventsReply(HelperReplyBase):
    """One page of a run's recorded events, oldest first."""

    type: Literal["runs.read_events"] = "runs.read_events"
    events: tuple[RunEventDocument, ...]
    next_sequence: PositiveInt
    """The ``from_sequence`` of the next page."""
    complete: bool
    """The page reached the end of what the journal holds now."""
    total_events: Annotated[int | None, Field(exclude_if=lambda total: total is None)] = None
    """The journal's complete lines when the reply was made; only with ``with_totals``."""
    total_bytes: Annotated[int | None, Field(exclude_if=lambda total: total is None)] = None
    """The bytes of those lines. Present with ``total_events``."""


class RunsEventsForSpanReply(HelperReplyBase):
    """One page of a span's recorded events, oldest first."""

    type: Literal["runs.events_for_span"] = "runs.events_for_span"
    events: tuple[RunEventDocument, ...]
    next_sequence: PositiveInt
    """The ``from_sequence`` of the next page."""
    complete: bool
    """The page reached the end of the span's events recorded now."""
    total: NonNegativeInt
    """How many events the span has recorded now, across every page: the denominator of a
    progress count, and 0 for a span that has none (or that the journal never named)."""


class RunsReadTailReply(HelperReplyBase):
    """The newest events before the request's ``before_sequence``, oldest first."""

    type: Literal["runs.read_tail"] = "runs.read_tail"
    events: tuple[RunEventDocument, ...]
    complete: bool
    """No older event is recorded: the page begins at the journal's first event."""
    last_sequence: NonNegativeInt
    """The journal's newest recorded sequence now, whatever page this is; 0 for an empty one."""
