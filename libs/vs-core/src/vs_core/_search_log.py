"""The search's side of the log: one record per advance, and a search restored from them.

An advance is the search hearing what core published and answering with proposals. It is one
record, ``StrategyAdvanced``, holding the proposals core took, the state change the search made
and the wake-ups it asked for, so a crash can never leave proposals without the state that
made them, or the state without its proposals. The log keeps the state's journal steps
(``SearchDriver.changes``); a whole state is logged at a restart, each time the advance count
reaches ``CHECKPOINT_GROWTH`` times the last checkpoint's, and when the run parks or ends, so
the log grows with the steps and a resume after a clean stop restarts from the latest state.
The fold of core never reads the search's bytes; ``restore`` does, through the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_decode.api import Unreadable, decode_json
from vs_proto.api import RUN_LOG_MAX_RECORD_BYTES, StrategyAdvanced

from ._search_run import Advanced, Inbox, SearchHost, advance, begin, deferred
from ._step import step
from ._step_log import SCHEMA_VERSION, decode_held, decode_snapshot, encode_event, encode_held
from .types.search import TimeReached

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import CoreSnapshot, Record

    from ._registry import OperationRegistry
    from ._search_events import Translated
    from ._search_run import SearchDriver
    from .types.kernel import CoreEvent, CoreState, Transition
    from .types.strategy import Decision, StrategyState

CHECKPOINT_GROWTH = 2
"""A whole state is logged when the advance count reaches this multiple of the last checkpoint's.

The log then holds about ``CHECKPOINT_GROWTH / (CHECKPOINT_GROWTH - 1)`` times the state in
checkpoints, however long the run, and a crash together with a code change restarts the flows
from a checkpoint at most ``1 - 1 / CHECKPOINT_GROWTH`` of the journal old. Raise it toward 1 to
shorten that age at the price of more log. Nothing else depends on its value."""


@dataclass(frozen=True)
class Progress[S: StrategyState]:
    """The search as the log leaves it: its state, the wake-ups it waits for, what it holds."""

    state: S
    wakes: tuple[float, ...] = ()
    """Run-clock times the search asked to be woken at and has not yet been."""
    held: tuple[Decision, ...] = ()
    """Decisions core could not take yet; they are offered again once it can."""
    advances: int = 0
    """The records the log holds for it."""
    checkpoint_at: int = 0
    """The record count at its last whole state."""
    checkpointed: bool = False
    """Whether ``state`` is exactly the whole state of the last record."""

    def next_wake(self) -> float | None:
        """The earliest wake-up, or None when the search asked for none."""
        return min(self.wakes, default=None)


@dataclass(frozen=True)
class Advancing[S: StrategyState]:
    """One advance: the record to append, and the search once the record is in the log."""

    record: StrategyAdvanced
    progress: Progress[S]


def restore[S: StrategyState, C](
    records: Iterable[Record], search: SearchDriver[S, C], registry: OperationRegistry | None
) -> Progress[S] | None:
    """The search the log's advance records leave, None before the first advance."""
    progress: Progress[S] | None = None
    for record in records:
        body = record.body
        if body is not None and body.field == "strategy_advanced":
            progress = _read(body.value, progress, search, registry)
        elif body is not None and body.field == "core_snapshot":
            progress = _saved(body.value, search, registry)
    return None if progress is None else replace(progress, state=search.resume(progress.state))


def advance_search[S: StrategyState, C](  # noqa: PLR0913  # lint-waiver: LW-441826 [PLR0913]; the search, its config and progress, and the core state with what it owes, are the five things one advance reads.
    # > A settings object for one call site adds a type, and the arguments are the data the
    # > advance is a function of, so grouping them would hide what it depends on.
    search: SearchDriver[S, C],
    config: C,
    progress: Progress[S] | None,
    core: CoreState,
    owed: Translated,
    now: float,
    registry: OperationRegistry,
) -> Advancing[S] | None:
    """The search hears ``owed`` and the wake-ups due at ``now``; None when it has nothing to hear.

    ``core`` is the state after the records that published ``owed``. The proposals core takes
    run on a copy of it, so the caller appends the record and its fold reaches the same state.
    """
    due = () if progress is None else tuple(sorted(w for w in progress.wakes if w <= now))
    held = () if progress is None else progress.held
    if progress is not None and not (
        owed.events or due or any(not deferred(core, decision) for decision in held)
    ):
        return None
    taken: list[CoreEvent] = []

    def record_step(state: CoreState, event: CoreEvent) -> Transition:
        taken.append(event)
        return step(state, event)

    host = SearchHost(step=record_step, registry=registry)
    arrived = Inbox((*owed.events, *(TimeReached(time=wake) for wake in due)), (), owed.discarded)
    if progress is None:
        advanced = begin(search, config, core, host, arrived)
    else:
        advanced = _hear_then_offer(search, progress, core, host, arrived)
    asked = {w for w in (progress.wakes if progress else ()) if w not in due} | set(advanced.wakes)
    count = 1 if progress is None else progress.advances + 1
    if progress is None:
        changes, whole, checkpoint_at = None, True, count
    else:
        changes = search.changes(progress.state, advanced.state)
        whole = changes is None or count >= CHECKPOINT_GROWTH * max(progress.checkpoint_at, 1)
        checkpoint_at = count if whole else progress.checkpoint_at
    record = StrategyAdvanced(
        schema_version=SCHEMA_VERSION,
        proposals=[encode_event(event) for event in taken],
        strategy=_strategy(advanced.state, changes, whole=whole),
        wakes=list(advanced.wakes),
        delivered=list(due),
        held=encode_held(advanced.held),
    )
    return Advancing(
        record,
        Progress(
            advanced.state,
            tuple(sorted(asked)),
            advanced.held,
            count,
            checkpoint_at,
            checkpointed=whole,
        ),
    )


def _hear_then_offer[S: StrategyState, C](
    search: SearchDriver[S, C],
    progress: Progress[S],
    core: CoreState,
    host: SearchHost,
    arrived: Inbox,
) -> Advanced[S]:
    """The search hears ``arrived``, and only then are the decisions it held offered to core.

    An event can restart the flows of the search (a resume lifts the stop it wound down under).
    The restarted flows ask again for what they need, so a decision the dead ones held is no
    longer wanted: it is offered only if the search still waits for its answer, and what it
    asks again goes first.
    """
    heard = advance(search, progress.state, core, host, arrived)
    wanted = tuple(d for d in progress.held if search.awaits(heard.state, d))
    if not wanted and not heard.held:
        return heard
    offered = advance(search, heard.state, heard.core, host, Inbox(held=(*wanted, *heard.held)))
    return replace(
        offered,
        discarded=(*heard.discarded, *offered.discarded),
        published=(*heard.published, *offered.published),
        wakes=(*heard.wakes, *offered.wakes),
        submitted=(*heard.submitted, *offered.submitted),
    )


def checkpoint_search[S: StrategyState](progress: Progress[S]) -> Advancing[S] | None:
    """The record that logs the search's whole state when the run parks or ends; None if it is."""
    if progress.checkpointed:
        return None
    record = StrategyAdvanced(
        schema_version=SCHEMA_VERSION,
        strategy=_strategy(progress.state, None, whole=True),
        held=encode_held(progress.held),
    )
    count = progress.advances + 1
    return Advancing(
        record, replace(progress, advances=count, checkpoint_at=count, checkpointed=True)
    )


def _strategy(
    state: StrategyState,
    changes: bytes | None,
    *,
    whole: bool,
) -> Oneof:
    if whole or changes is None:
        return Oneof("checkpoint", state.model_dump_json().encode())
    return Oneof("steps", changes)


def _whole_state[S: StrategyState, C](
    search: SearchDriver[S, C], encoded: bytes, *, max_bytes: int
) -> S:
    """The search's whole state as it is logged: in the search driver's own encoding."""
    decoded = decode_json(
        encoded, search.state_model, source="the search checkpoint in the log", max_bytes=max_bytes
    )
    match decoded:
        case Unreadable():
            raise ValueError(str(decoded))
        case _:
            return decoded


def _saved[S: StrategyState, C](
    message: CoreSnapshot, search: SearchDriver[S, C], registry: OperationRegistry | None
) -> Progress[S]:
    """The search a run an older version saved left: its whole state, as a checkpoint is."""
    saved = decode_snapshot(message, registry)
    state = _whole_state(search, saved.strategy, max_bytes=max(len(saved.strategy), 1))
    return Progress(
        state,
        tuple(sorted(saved.wakes)),
        saved.held,
        advances=1,
        checkpoint_at=1,
        checkpointed=True,
    )


def _read[S: StrategyState, C](
    message: StrategyAdvanced,
    progress: Progress[S] | None,
    search: SearchDriver[S, C],
    registry: OperationRegistry | None,
) -> Progress[S]:
    match message.strategy:
        case Oneof(field="checkpoint", value=whole):
            state = _whole_state(search, whole, max_bytes=RUN_LOG_MAX_RECORD_BYTES)
            whole_state = True
        case Oneof(field="steps", value=changes) if progress is not None:
            state = search.extend(progress.state, changes)
            whole_state = False
        case _:
            text = "the log extends a search state it holds no checkpoint of"
            raise ValueError(text)
    count = 1 if progress is None else progress.advances + 1
    kept = {w for w in (progress.wakes if progress else ()) if w not in set(message.delivered)}
    held = decode_held(message, registry)
    return Progress(
        state,
        tuple(sorted(kept | set(message.wakes))),
        held,
        count,
        count if whole_state else progress.checkpoint_at if progress else count,
        checkpointed=whole_state,
    )
