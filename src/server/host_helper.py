"""The host helper's channel: one light program per channel that answers the desktop app.

The desktop app runs ``python -m entrypoints.host_helper`` once per channel, locally
over stdio or remotely over one ssh exec channel. A channel carries typed JSON lines
(``server.host_api``): a ``hello`` first, then requests answered concurrently, each
reply echoing its request's id. A run channel ends with ``run.connect``, after which
it is a byte relay to the run's control socket (``server.relay``).
``docs/contributing/desktop-connection.md`` ("The host helper") owns the design.

The module is a pure core and a thin shell; answering one request is
``server.host_helper_requests``' job:

- :func:`step` folds one observation of a channel (a line arrived, a request was
  answered, a watch observed the registry, time passed, input ended) into
  :class:`ChannelState` and returns the outputs the shell performs: lines to send,
  requests to execute, watches to start or stop, a connection to relay or release.
  It owns the handshake, request-id uniqueness, deadlines, admission limits,
  resource settlement, feature gating, and the one hand-over of a run channel.
- :func:`serve_channel` is the shell: one reader of the client's bytes, one worker
  per request (``answer`` or ``connect``), one watcher per watch, and the supervisor
  that runs :func:`step`.

Like every host command it is light: ``tests/architecture/test_light_entrypoints.py``
holds its imports to the standard library, Pydantic, ``vs_sim.api``,
``vs_project.api``, and light ``server`` modules.

Public surface: ``step``, ``ChannelState``, ``ChannelEvent`` and its members,
``ChannelOutput`` and its members, ``accepts_input``, ``next_deadline``;
``serve_channel``; and the limits ``MAX_EXECUTING_REQUESTS``, ``MAX_ACTIVE_WATCHES``,
``WATCH_INTERVAL_SECONDS`` and ``MAX_LINE_BYTES``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, assert_never

from pydantic import JsonValue, TypeAdapter

from server.host_api import (
    COMPENSATION_FAILURE_DETAIL_CHARACTERS,
    COMPENSATION_FAILURE_EXIT_STATUS,
    COMPENSATION_FAILURE_TYPE,
    HELPER_LINE_MAX_BYTES,
    CompensationFailureKind,
    HelloRequest,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperMessage,
    HelperReply,
    HelperRequest,
    InstanceList,
    InstancesChanged,
    InstanceStopResult,
    InstancesUnwatchReply,
    InstancesUnwatchRequest,
    InstancesWatchReply,
    InstancesWatchRequest,
    LiveInstanceRecord,
    LogsCancelReply,
    LogsCancelRequest,
    LogsReadRequest,
    RunConnectReply,
    RunConnectRequest,
    RunStartCompensationFailure,
    RunStartReply,
    RunStartRequest,
    StopFailedCompensationFailure,
    StopOutcome,
    StopUnconfirmedCompensationFailure,
    UnconfirmedInstanceStopResult,
    WorkerStartRefusedCompensationFailure,
    parse_helper_request,
    request_features,
)
from server.host_helper_requests import (
    HelperHost,
    Negotiated,
    answer,
    connect,
    negotiate,
    without_home,
)
from server.relay import (
    ChannelEndOutcome,
    ClientStreams,
    relay,
)
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Mapping

    from server.relay import ByteSink, ByteSource
    from vs_sim.api import Connection, Event, ReservableThreads, Threads

WATCH_INTERVAL_SECONDS: Final = 1.0
"""How often a watch reads the registry; a change is pushed at most this late."""
MAX_EXECUTING_REQUESTS: Final = 16
"""The most request or compensation workers one channel owns at once."""
MAX_ACTIVE_WATCHES: Final = 1
"""The most active or physically stopping watch workers one channel owns at once."""
MAX_LINE_BYTES: Final = HELPER_LINE_MAX_BYTES
"""The longest request line; a longer one is read in pieces, each answered ``invalid_request``."""
_READ_BYTES: Final = 64 * 1024
_MAX_ERROR_DETAIL: Final = 2000
_MAX_REQUEST_ID: Final = 128
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


# ----- the pure core: one channel -----


@dataclass(frozen=True)
class ChannelState:
    """What one channel knows: the handshake, requests in flight, watches, the hand-over."""

    offered: frozenset[HelperFeature] = frozenset(HelperFeature)
    """The features this helper serves (``HelperHost.offered_features``); ``hello`` negotiates among them."""
    negotiated: Negotiated | None = None
    pending: Mapping[str, float] = field(default_factory=dict)
    """Replies still owed to the client, by id, with their deadline."""
    executing: frozenset[str] = frozenset()
    """Request workers that have not physically returned, including expired ones."""
    starting: frozenset[str] = frozenset()
    """``run.start`` workers still owned after their client deadline passes."""
    watches: Mapping[str, InstanceList | None] = field(default_factory=dict)
    """Open watches, by id, with the listing last pushed on each."""
    stopping_watches: frozenset[str] = frozenset()
    """Watch ids owned until their workers confirm that they stopped."""
    compensating: Mapping[str, str] = field(default_factory=dict)
    """Undeliverable started runs still owned, from request id to instance id."""
    compensation_failures: tuple[RunStartCompensationFailure, ...] = ()
    """Terminal diagnostics for compensating stops that could not be proved complete."""
    reads: frozenset[str] = frozenset()
    """``logs.read`` workers that have not returned: the ones ``logs.cancel`` can withdraw."""
    withdrawing: Mapping[str, str] = field(default_factory=dict)
    """Each withdrawn read's id, with the ``logs.cancel`` answered once the read returns."""
    connecting: str | None = None
    """The ``run.connect`` in flight; input waits until it is answered."""
    relaying: bool = False
    """The channel was handed to the relay; nothing else happens on it."""
    input_ended: bool = False


@dataclass(frozen=True)
class Received:
    """One complete line arrived at time ``at``."""

    line: bytes
    at: float


@dataclass(frozen=True)
class Answered:
    """An executed request's reply is ready."""

    request_id: str
    reply: HelperReply | HelperError


@dataclass(frozen=True)
class Observed:
    """A watch read the registry."""

    watch_id: str
    listing: InstanceList


@dataclass(frozen=True)
class WatchStopped:
    """The watch worker for ``watch_id`` physically returned."""

    watch_id: str


@dataclass(frozen=True)
class StartUndeliverable:
    """A successful start reply could not be delivered and now needs compensation."""

    request_id: str
    instance_id: str


@dataclass(frozen=True)
class CompensationFinished:
    """The cleanup owned by ``request_id`` returned a typed stop result."""

    request_id: str
    result: InstanceStopResult


@dataclass(frozen=True)
class CompensationFailed:
    """The cleanup worker could not return a typed stop result."""

    request_id: str
    instance_id: str
    kind: CompensationFailureKind
    detail: str


@dataclass(frozen=True)
class Tick:
    """Time has reached ``at``."""

    at: float


@dataclass(frozen=True)
class InputEnded:
    """The client's input reached end of stream."""


type ChannelEvent = (
    Received
    | Answered
    | Observed
    | WatchStopped
    | StartUndeliverable
    | CompensationFinished
    | CompensationFailed
    | Tick
    | InputEnded
)


@dataclass(frozen=True)
class StopStartedRun:
    """Stop a newly started instance whose identity cannot reach the client."""

    request_id: str
    instance_id: str


@dataclass(frozen=True)
class Send:
    """Write one message line to the client, compensating an undeliverable start."""

    message: HelperMessage
    if_undelivered: StopStartedRun | None = None


@dataclass(frozen=True)
class Execute:
    """Run ``request`` through the host and report ``Answered``."""

    request: HelperRequest


@dataclass(frozen=True)
class Watch:
    """Start reporting ``Observed`` for ``watch_id``."""

    watch_id: str


@dataclass(frozen=True)
class Unwatch:
    """Stop reporting for ``watch_id``."""

    watch_id: str


@dataclass(frozen=True)
class WithdrawRead:
    """End the wait of the ``logs.read`` ``request_id`` executes, so it answers at once."""

    request_id: str


@dataclass(frozen=True)
class Relay:
    """Hand the channel to the relay over the connection ``request_id`` opened."""

    request_id: str


@dataclass(frozen=True)
class Release:
    """Close the connection ``request_id`` opened; its reply came too late."""

    request_id: str


@dataclass(frozen=True)
class Close:
    """The channel is done: input ended and nothing is in flight."""


type ChannelOutput = (
    Send | Execute | Watch | Unwatch | WithdrawRead | Relay | Release | StopStartedRun | Close
)


def accepts_input(state: ChannelState) -> bool:
    """Whether the shell should read lines: not while a run channel is being handed over."""
    return state.connecting is None and not state.relaying and not state.input_ended


def next_deadline(state: ChannelState) -> float | None:
    """When the earliest request in flight runs out of time."""
    return min(state.pending.values(), default=None)


def step(
    state: ChannelState, event: ChannelEvent, vibesys_version: str
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Fold one observation into the channel; return the new state and the outputs."""
    if state.relaying and not isinstance(event, (CompensationFinished, CompensationFailed)):
        return state, ()
    match event:
        case Received(line=line, at=at):
            return _received(state, line, at, vibesys_version)
        case Answered(request_id=request_id, reply=reply):
            return _answered(state, request_id, reply)
        case Observed(watch_id=watch_id, listing=listing):
            return _observed(state, watch_id, listing)
        case (
            WatchStopped()
            | StartUndeliverable()
            | CompensationFinished()
            | CompensationFailed()
            | InputEnded()
        ):
            return _settle(state, event)
        case Tick(at=at):
            return _tick(state, at)
        case _:
            assert_never(event)


def _settle(
    state: ChannelState,
    event: WatchStopped
    | StartUndeliverable
    | CompensationFinished
    | CompensationFailed
    | InputEnded,
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Release physical ownership only after a worker reports semantic completion."""
    match event:
        case WatchStopped(watch_id=watch_id):
            watches = {wid: last for wid, last in state.watches.items() if wid != watch_id}
            state = replace(
                state,
                watches=watches,
                stopping_watches=state.stopping_watches - {watch_id},
            )
            return _maybe_close(state, [])
        case StartUndeliverable(request_id=request_id, instance_id=instance_id):
            if request_id in state.compensating:
                return state, ()
            state = replace(state, compensating={**state.compensating, request_id: instance_id})
            return state, (StopStartedRun(request_id, instance_id),)
        case CompensationFinished() | CompensationFailed():
            return _settle_compensation(state, event)
        case InputEnded():
            outputs: list[ChannelOutput] = [Unwatch(watch_id) for watch_id in state.watches]
            state = replace(
                state,
                input_ended=True,
                stopping_watches=state.stopping_watches | frozenset(state.watches),
                watches={},
            )
            return _maybe_close(state, outputs)
        case _:
            assert_never(event)


def _settle_compensation(
    state: ChannelState, event: CompensationFinished | CompensationFailed
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Turn one returned cleanup worker into terminal release or retained failure evidence."""
    request_id = event.request_id
    instance_id = state.compensating.get(request_id)
    if instance_id is None:
        return state, ()
    state = replace(
        state,
        compensating={
            owned_request: owned_instance
            for owned_request, owned_instance in state.compensating.items()
            if owned_request != request_id
        },
    )
    if isinstance(event, CompensationFinished):
        result = event.result
        if result.id != instance_id:
            failure = _compensation_report(
                request_id=request_id,
                instance_id=instance_id,
                kind=CompensationFailureKind.STOP_FAILED,
                detail=f"compensating stop returned the wrong instance id {result.id}",
            )
            return _record_compensation_failure(state, failure)
        if result.outcome in {StopOutcome.STOPPED, StopOutcome.NOT_RUNNING}:
            return _maybe_close(state, [])
        failure = _compensation_report(
            request_id=request_id,
            instance_id=result.id,
            kind=CompensationFailureKind.STOP_UNCONFIRMED,
            detail=f"compensating stop returned {result.outcome.value}",
            stop_result=result,
        )
        return _record_compensation_failure(state, failure)
    kind = event.kind
    detail = event.detail
    if event.instance_id != instance_id:
        kind = CompensationFailureKind.STOP_FAILED
        detail = f"compensation failure named the wrong instance id {event.instance_id}"
    failure = _compensation_report(
        request_id=request_id,
        instance_id=instance_id,
        kind=kind,
        detail=detail,
    )
    return _record_compensation_failure(state, failure)


def _record_compensation_failure(
    state: ChannelState, failure: RunStartCompensationFailure
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Retain cleanup evidence and end this channel through its ordinary bounded drain."""
    stopping = frozenset(state.watches)
    outputs: list[ChannelOutput] = [Unwatch(watch_id) for watch_id in state.watches]
    state = replace(
        state,
        input_ended=True,
        watches={},
        stopping_watches=state.stopping_watches | stopping,
        compensation_failures=(*state.compensation_failures, failure),
    )
    return _maybe_close(state, outputs)


def _observed(
    state: ChannelState, watch_id: str, listing: InstanceList
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Push a watch's listing when it is the first or differs from the last one pushed."""
    if watch_id not in state.watches or state.watches[watch_id] == listing:
        return state, ()
    watches = {**state.watches, watch_id: listing}
    pushed = Send(InstancesChanged(watch_id=watch_id, listing=listing))
    return replace(state, watches=watches), (pushed,)


def _tick(state: ChannelState, at: float) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Answer ``deadline_exceeded`` for every request whose deadline has passed."""
    expired = sorted(rid for rid, deadline in state.pending.items() if deadline <= at)
    if not expired:
        return state, ()
    pending = {rid: d for rid, d in state.pending.items() if rid not in expired}
    connecting = None if state.connecting in expired else state.connecting
    outputs: list[ChannelOutput] = [
        Send(_error(rid, HelperErrorCode.DEADLINE_EXCEEDED, "no reply within the deadline"))
        for rid in expired
    ]
    return _maybe_close(replace(state, pending=pending, connecting=connecting), outputs)


def _maybe_close(
    state: ChannelState, outputs: list[ChannelOutput]
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if (
        state.input_ended
        and not state.pending
        and not state.starting
        and not state.stopping_watches
        and not state.compensating
    ):
        outputs.append(Close())
    return state, tuple(outputs)


def _error(request_id: str | None, code: HelperErrorCode, message: str) -> HelperError:
    return HelperError(request_id=request_id, code=code, message=message)


def _request_id_of(line: bytes) -> str | None:
    """The ``request_id`` of a line that is not a valid request, when it carries a usable one."""
    document = decode_json(line, _JSON_VALUE, source="request", max_bytes=MAX_LINE_BYTES)
    if isinstance(document, Unreadable):
        return None
    value = document.get("request_id") if isinstance(document, dict) else None
    if isinstance(value, str) and 0 < len(value) <= _MAX_REQUEST_ID:
        return value
    return None


def _received(
    state: ChannelState, line: bytes, at: float, vibesys_version: str
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if not accepts_input(state):
        return state, ()
    request = parse_helper_request(line)
    if isinstance(request, Unreadable):
        detail = request.details("request")[:_MAX_ERROR_DETAIL]
        refused = _error(_request_id_of(line), HelperErrorCode.INVALID_REQUEST, detail)
        return state, (Send(refused),)
    state, refusal = _admission(state, request, vibesys_version)
    if refusal is not None:
        return state, (Send(refusal),)
    return _admit(state, request, at)


def _admission(
    state: ChannelState, request: HelperRequest, vibesys_version: str
) -> tuple[ChannelState, HelperError | None]:
    """Whether the channel takes ``request`` now; a successful ``hello`` negotiates here."""
    request_id = request.request_id
    refusal: HelperError | None = None
    if state.negotiated is None:
        if isinstance(request, HelloRequest):
            negotiated = negotiate(request, vibesys_version, state.offered)
            if isinstance(negotiated, HelperError):
                refusal = negotiated
            else:
                state = replace(state, negotiated=negotiated)
        else:
            message = "the first request on a channel must be hello"
            refusal = _error(request_id, HelperErrorCode.HANDSHAKE_REQUIRED, message)
    elif isinstance(request, HelloRequest):
        message = "hello was already negotiated on this channel"
        refusal = _error(request_id, HelperErrorCode.INCOMPATIBLE, message)
    elif (
        request_id in state.pending
        or request_id in state.executing
        or request_id in state.starting
        or request_id in state.watches
        or request_id in state.stopping_watches
        or request_id in state.compensating
    ):
        message = f"request id {request_id!r} is already open on this channel"
        refusal = _error(request_id, HelperErrorCode.DUPLICATE_REQUEST_ID, message)
    else:
        missing = [f for f in request_features(request) if f not in state.negotiated.features]
        if missing:
            message = (
                f"{request.type} needs the {missing[0].value} feature, which hello did not return"
            )
            refusal = _error(request_id, HelperErrorCode.FEATURE_NOT_NEGOTIATED, message)
        elif isinstance(request, InstancesWatchRequest) and (
            len(state.watches) + len(state.stopping_watches) >= MAX_ACTIVE_WATCHES
        ):
            message = f"this channel already owns {MAX_ACTIVE_WATCHES} watch worker"
            refusal = _error(request_id, HelperErrorCode.CHANNEL_BUSY, message)
        elif not isinstance(request, InstancesUnwatchRequest | LogsCancelRequest) and (
            len(state.executing) + len(state.compensating) >= MAX_EXECUTING_REQUESTS
        ):
            message = f"this channel already owns {MAX_EXECUTING_REQUESTS} request workers"
            refusal = _error(request_id, HelperErrorCode.CHANNEL_BUSY, message)
    return state, refusal


def _admit(
    state: ChannelState, request: HelperRequest, at: float
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    request_id = request.request_id
    match request:
        case InstancesWatchRequest():
            watches = {**state.watches, request_id: None}
            return replace(state, watches=watches), (
                Watch(request_id),
                Send(InstancesWatchReply(request_id=request_id, watch_id=request_id)),
            )
        case InstancesUnwatchRequest(watch_id=watch_id):
            if watch_id not in state.watches:
                message = f"no watch {watch_id!r} is open on this channel"
                return state, (Send(_error(request_id, HelperErrorCode.UNKNOWN_WATCH, message)),)
            watches = {wid: last for wid, last in state.watches.items() if wid != watch_id}
            state = replace(
                state,
                watches=watches,
                stopping_watches=state.stopping_watches | {watch_id},
            )
            return state, (
                Unwatch(watch_id),
                Send(InstancesUnwatchReply(request_id=request_id, watch_id=watch_id)),
            )
        case LogsCancelRequest():
            return _withdraw(state, request, at)
        case LogsReadRequest():
            state = replace(state, reads=state.reads | {request_id})
        case RunConnectRequest():
            if state.executing or state.watches or state.stopping_watches:
                message = "run.connect needs a channel with no other request or watch open"
                return state, (Send(_error(request_id, HelperErrorCode.CHANNEL_BUSY, message)),)
            state = replace(state, connecting=request_id)
        case RunStartRequest():
            state = replace(state, starting=state.starting | {request_id})
        case _:
            pass
    pending = {**state.pending, request_id: at + request.deadline_ms / 1000}
    executing = state.executing | {request_id}
    return replace(state, pending=pending, executing=executing), (Execute(request),)


def _withdraw(
    state: ChannelState, request: LogsCancelRequest, at: float
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """End the wait of the read ``request`` names; its reply follows the read's (``_withdrawn``)."""
    request_id, read_id = request.request_id, request.read_id
    if read_id not in state.reads or read_id in state.withdrawing:
        return state, (Send(LogsCancelReply(request_id=request_id, read_id=read_id)),)
    pending = {**state.pending, request_id: at + request.deadline_ms / 1000}
    withdrawing = {**state.withdrawing, read_id: request_id}
    return replace(state, pending=pending, withdrawing=withdrawing), (WithdrawRead(read_id),)


def _answered(
    state: ChannelState, request_id: str, reply: HelperReply | HelperError
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if request_id not in state.executing:
        return state, ()
    was_starting = request_id in state.starting
    state, withdrawn = _withdrawn(
        replace(
            state,
            executing=state.executing - {request_id},
            starting=state.starting - {request_id},
            reads=state.reads - {request_id},
        ),
        request_id,
    )
    compensation = _start_compensation(request_id, reply) if was_starting else None
    if request_id not in state.pending:
        # Its deadline already answered for it; resources it created have no taker.
        outputs: list[ChannelOutput] = []
        if isinstance(reply, RunConnectReply):
            outputs.append(Release(request_id))
        elif compensation is not None:
            state = replace(
                state,
                compensating={
                    **state.compensating,
                    request_id: compensation.instance_id,
                },
            )
            outputs.append(compensation)
        return _maybe_close(state, [*outputs, *withdrawn])
    pending = {rid: d for rid, d in state.pending.items() if rid != request_id}
    state = replace(state, pending=pending)
    outputs = [Send(reply, if_undelivered=compensation), *withdrawn]
    if request_id == state.connecting:
        if isinstance(reply, RunConnectReply):
            return replace(state, connecting=None, relaying=True), (*outputs, Relay(request_id))
        state = replace(state, connecting=None)
    return _maybe_close(state, outputs)


def _withdrawn(state: ChannelState, read_id: str) -> tuple[ChannelState, list[ChannelOutput]]:
    """Answer the ``logs.cancel`` that withdrew ``read_id``, now that the read returned.

    After the read's own reply, so a client that sees the cancel's reply knows the
    read's worker is free; nothing when its deadline already answered for it.
    """
    cancel_id = state.withdrawing.get(read_id)
    if cancel_id is None:
        return state, []
    withdrawing = {rid: cid for rid, cid in state.withdrawing.items() if rid != read_id}
    state = replace(state, withdrawing=withdrawing)
    if cancel_id not in state.pending:
        return state, []
    pending = {rid: d for rid, d in state.pending.items() if rid != cancel_id}
    reply = LogsCancelReply(request_id=cancel_id, read_id=read_id)
    return replace(state, pending=pending), [Send(reply)]


def _start_compensation(request_id: str, reply: HelperReply | HelperError) -> StopStartedRun | None:
    """Return the cleanup owned by a newly created, undeliverable start reply."""
    if isinstance(reply, RunStartReply) and isinstance(reply.launch, LiveInstanceRecord):
        return StopStartedRun(request_id, reply.launch.id)
    return None


# ----- the shell -----


class _Inbox:
    """The client's bytes, read by one pump and taken by lines or, once relaying, as bytes."""

    def __init__(self, threads: Threads) -> None:
        self.cond = threads.condition()
        self.buffer = bytearray()
        self.ended = False

    def pump(self, source: ByteSource) -> None:
        """Read the client until end of stream, holding at most ``MAX_LINE_BYTES`` unread."""
        while True:
            with self.cond:
                self.cond.wait_for(lambda: len(self.buffer) < MAX_LINE_BYTES)
            try:
                chunk = source.read(_READ_BYTES)
            except OSError:
                chunk = b""
            with self.cond:
                if chunk:
                    self.buffer += chunk
                else:
                    self.ended = True
                self.cond.notify_all()
            if not chunk:
                return

    def take_line(self) -> bytes | None:
        """The next complete line, or an oversized prefix; call with ``cond`` held."""
        end = self.buffer.find(b"\n")
        if end < 0:
            if len(self.buffer) < MAX_LINE_BYTES:
                return None
            end = len(self.buffer) - 1
        line = bytes(self.buffer[:end])
        del self.buffer[: end + 1]
        self.cond.notify_all()
        return line


class _InboxSource:
    """The relay's view of the client: what the inbox holds, then what the pump reads."""

    def __init__(self, inbox: _Inbox) -> None:
        self._inbox = inbox

    def read(self, max_bytes: int) -> bytes:
        inbox = self._inbox
        with inbox.cond:
            inbox.cond.wait_for(lambda: bool(inbox.buffer) or inbox.ended)
            chunk = bytes(inbox.buffer[:max_bytes])
            del inbox.buffer[:max_bytes]
            inbox.cond.notify_all()
            return chunk


@dataclass(frozen=True)
class _ChannelClosed:
    """A control channel ended, including any retained compensation failures."""

    compensation_failures: tuple[RunStartCompensationFailure, ...]


class _Channel:
    def __init__(
        self, host: HelperHost, inbox: _Inbox, output: ByteSink, threads: ReservableThreads
    ) -> None:
        self._host = host
        self._inbox = inbox
        self._output = output
        self._threads = threads
        self._state = ChannelState(offered=host.offered_features())
        self._events: list[ChannelEvent] = []
        self._held: dict[str, Connection] = {}
        self._watches: dict[str, tuple[Event, Event]] = {}
        self._withdrawals: dict[str, Event] = {}
        self._handed_over: str | None = None
        self._closed = False

    def post(self, event: ChannelEvent) -> None:
        with self._inbox.cond:
            self._events.append(event)
            self._inbox.cond.notify_all()

    def supervise(self) -> Connection | _ChannelClosed:
        """Run until control closes with evidence, or a connection is handed to the relay."""
        cond = self._inbox.cond
        with cond:
            while True:
                if not self._closed:
                    self._take_input()
                while self._events:
                    self._apply(self._events.pop(0))
                if (
                    self._closed
                    and not self._state.starting
                    and not self._state.stopping_watches
                    and not self._state.compensating
                ):
                    self._close_held()
                    return _ChannelClosed(self._state.compensation_failures)
                if self._handed_over is not None:
                    if not self._state.compensating:
                        if self._state.compensation_failures:
                            self._close_held()
                            return _ChannelClosed(self._state.compensation_failures)
                        return self._held.pop(self._handed_over)
                    # The relay owns the channel now. Only compensation can delay
                    # handoff, and its worker notifies this condition when it settles.
                    cond.wait()
                    continue
                deadline = next_deadline(self._state)
                if not self._events and not self._input_ready():
                    timeout = None if deadline is None else max(deadline - self._threads.now(), 0.0)
                    cond.wait(timeout)
                self._events.append(Tick(self._threads.now()))

    def _close_held(self) -> None:
        """Release connections a failed channel can no longer hand to a relay."""
        for connection in self._held.values():
            connection.close()
        self._held.clear()

    def _input_ready(self) -> bool:
        inbox = self._inbox
        if self._closed or not accepts_input(self._state):
            return False
        return b"\n" in inbox.buffer or len(inbox.buffer) >= MAX_LINE_BYTES or inbox.ended

    def _take_input(self) -> None:
        while accepts_input(self._state):
            line = self._inbox.take_line()
            if line is None:
                if self._inbox.ended:
                    self._apply(InputEnded())
                return
            self._apply(Received(line, self._threads.now()))

    def _apply(self, event: ChannelEvent) -> None:
        self._state, outputs = step(self._state, event, self._host.vibesys_version)
        try:
            for output in outputs:
                self._perform(output)
        finally:
            if isinstance(event, Observed):
                watch = self._watches.get(event.watch_id)
                if watch is not None:
                    watch[1].set()
            elif isinstance(event, WatchStopped):
                self._watches.pop(event.watch_id, None)
            elif isinstance(event, Answered):
                self._withdrawals.pop(event.request_id, None)

    def _perform(self, output: ChannelOutput) -> None:
        match output:
            case Send():
                self._send(output)
            case Execute(request=request):
                self._begin_execute(request)
            case WithdrawRead(request_id=request_id):
                self._withdrawals[request_id].set()
            case Watch(watch_id=watch_id):
                stop = self._threads.event()
                observed = self._threads.event()
                self._watches[watch_id] = (stop, observed)
                self._threads.spawn(
                    lambda: self._watch(watch_id, stop, observed), name="helper-watch"
                )
            case Unwatch(watch_id=watch_id):
                self._watches[watch_id][0].set()
            case Relay(request_id=request_id):
                self._handed_over = request_id
            case Release(request_id=request_id):
                self._held.pop(request_id).close()
            case StopStartedRun(request_id=request_id, instance_id=instance_id):
                self._begin_stop_started(request_id, instance_id)
            case Close():
                self._closed = True
            case _:
                assert_never(output)

    def _send(self, output: Send) -> None:
        try:
            self._output.write(output.message.model_dump_json().encode() + b"\n")
        except OSError:
            # The client stopped reading: nobody is left to answer.
            if output.if_undelivered is not None:
                cleanup = output.if_undelivered
                self._apply(StartUndeliverable(cleanup.request_id, cleanup.instance_id))
            if not self._state.input_ended:
                self._apply(InputEnded())
            self._closed = True

    def _begin_execute(self, request: HelperRequest) -> None:
        """Spawn ``request``'s worker; a ``logs.read`` keeps its withdrawal for ``WithdrawRead``."""
        withdrawal = self._threads.event()
        if isinstance(request, LogsReadRequest):
            self._withdrawals[request.request_id] = withdrawal
        self._threads.spawn(
            lambda: self._execute(request, withdrawal), name=f"helper-{request.type}"
        )

    def _begin_stop_started(self, request_id: str, instance_id: str) -> None:
        try:
            self._threads.spawn(
                lambda: self._stop_started(request_id, instance_id), name="helper-stop-started"
            )
        # lint-waiver: LW-276801 [BLE001]; worker creation is the cleanup boundary.
        # > Catching Exception or only today's WorkerStartRefusedError would miss a
        # > BaseException exit from another Threads implementation, while a finally block
        # > has no failure detail. Converting every refusal preserves recorded ownership.
        except BaseException as error:  # noqa: BLE001
            failure = _compensation_failed_event(
                request_id,
                instance_id,
                CompensationFailureKind.WORKER_START_REFUSED,
                error,
            )
            self.post(failure)

    def _execute(self, request: HelperRequest, withdrawal: Event) -> None:
        try:
            if isinstance(request, RunConnectRequest):
                opened = connect(request, self._host)
                if isinstance(opened, HelperError):
                    reply: HelperReply | HelperError = opened
                else:
                    reply, connection = opened
                    with self._inbox.cond:
                        self._held[request.request_id] = connection
            else:
                reply = answer(request, self._host, withdrawal)
        # lint-waiver: LW-188705 [BLE001]; one request's failure is its own typed reply.
        # > Letting it escape kills the worker silently, and the client waits out its whole
        # > deadline for a `deadline_exceeded` that hides the cause; catching named types
        # > instead would miss exactly the unexpected failures this exists to report.
        except Exception as error:  # noqa: BLE001
            reply = _error(request.request_id, HelperErrorCode.INTERNAL, _exception_detail(error))
        self.post(Answered(request.request_id, without_home(reply, self._host)))

    def _watch(self, watch_id: str, stop: Event, observed: Event) -> None:
        try:
            while not stop.is_set():
                try:
                    listing = self._host.registry.read()
                except OSError:
                    return
                self.post(Observed(watch_id, listing))
                observed.wait()
                observed.clear()
                stop.wait(WATCH_INTERVAL_SECONDS)
        finally:
            self.post(WatchStopped(watch_id))

    def _stop_started(self, request_id: str, instance_id: str) -> None:
        try:
            result = self._host.registry.stop(instance_id, self._host.stop_effects)
        # lint-waiver: LW-276802 [BLE001]; every cleanup failure must settle ownership.
        # > Catching Exception or named registry errors misses BaseException exits such as
        # > SystemExit; a finally block would falsely report success without evidence.
        # > Converting the boundary failure keeps the supervisor joinable and explicit.
        except BaseException as error:  # noqa: BLE001
            failure = _compensation_failed_event(
                request_id,
                instance_id,
                CompensationFailureKind.STOP_FAILED,
                error,
            )
            self.post(failure)
        else:
            self.post(CompensationFinished(request_id, result))


def _compensation_failed_event(
    request_id: str,
    instance_id: str,
    kind: CompensationFailureKind,
    error: BaseException,
) -> CompensationFailed:
    """Bound one external failure for the channel core to settle."""
    return CompensationFailed(
        request_id,
        instance_id,
        kind,
        _exception_detail(error),
    )


def _exception_detail(error: BaseException) -> str:
    """Format an arbitrary failure without letting its formatter escape the boundary."""
    error_type = type(error).__name__
    try:
        message = str(error)
    # lint-waiver: LW-276803 [BLE001]; exception text is itself an untrusted boundary.
    # > Catching Exception misses BaseException raised by an arbitrary __str__, while
    # > repr(error) invokes another arbitrary formatter. The stable type name is the
    # > only diagnostic that does not execute code supplied by the caught exception.
    except BaseException:  # noqa: BLE001
        return error_type[:COMPENSATION_FAILURE_DETAIL_CHARACTERS]
    detail = f"{error_type}: {message}" if message else error_type
    return detail[:COMPENSATION_FAILURE_DETAIL_CHARACTERS]


def _compensation_report(
    *,
    request_id: str,
    instance_id: str,
    kind: CompensationFailureKind,
    detail: str,
    stop_result: InstanceStopResult | None = None,
) -> RunStartCompensationFailure:
    """Build the one strict external report for every compensation failure path."""
    bounded_detail = detail[:COMPENSATION_FAILURE_DETAIL_CHARACTERS] or kind.value
    if kind is CompensationFailureKind.WORKER_START_REFUSED:
        if stop_result is not None:
            message = "worker-start refusal cannot carry a stop result"
            raise ValueError(message)
        evidence = WorkerStartRefusedCompensationFailure(
            type=COMPENSATION_FAILURE_TYPE,
            exit_status=COMPENSATION_FAILURE_EXIT_STATUS,
            request_id=request_id,
            kind=kind,
            instance_id=instance_id,
            detail=bounded_detail,
        )
    elif kind is CompensationFailureKind.STOP_FAILED:
        if stop_result is not None:
            message = "failed stop cannot carry a stop result"
            raise ValueError(message)
        evidence = StopFailedCompensationFailure(
            type=COMPENSATION_FAILURE_TYPE,
            exit_status=COMPENSATION_FAILURE_EXIT_STATUS,
            request_id=request_id,
            kind=kind,
            instance_id=instance_id,
            detail=bounded_detail,
        )
    else:
        if stop_result is None:
            message = "unconfirmed stop needs its typed stop result"
            raise ValueError(message)
        if stop_result.id != instance_id:
            message = "unconfirmed stop result names another instance"
            raise ValueError(message)
        evidence = StopUnconfirmedCompensationFailure(
            type=COMPENSATION_FAILURE_TYPE,
            exit_status=COMPENSATION_FAILURE_EXIT_STATUS,
            request_id=request_id,
            kind=kind,
            detail=bounded_detail,
            stop_result=UnconfirmedInstanceStopResult.model_validate(stop_result.model_dump()),
        )
    return RunStartCompensationFailure(root=evidence)


def serve_channel(
    *,
    host: HelperHost,
    source: ByteSource,
    output: ByteSink,
    errors: ByteSink,
    threads: ReservableThreads,
) -> int:
    """Serve one channel until its input ends or it is relayed; the process exit status.

    ``0`` when the client ended a control channel. A control channel whose
    undeliverable successful start could not be proved stopped returns
    ``COMPENSATION_FAILURE_EXIT_STATUS`` and writes its typed
    ``RunStartCompensationFailure`` evidence to ``errors``. A relayed channel ends
    with the relay's status and, when that is not ``0``, one ``ChannelEnd`` line on
    ``errors``. A ``run.start`` worker and any compensation it owns settle before
    this returns; other workers and the input pump may still be blocked, and the
    caller ends the process.
    """
    inbox = _Inbox(threads)
    threads.spawn(lambda: inbox.pump(source), name="helper-input")
    channel = _Channel(host, inbox, output, threads)
    completion = channel.supervise()
    if isinstance(completion, _ChannelClosed):
        for failure in completion.compensation_failures:
            errors.write(failure.line().encode())
        return (
            completion.compensation_failures[0].exit_status
            if completion.compensation_failures
            else 0
        )
    connection = completion
    result = relay(connection, client=ClientStreams(_InboxSource(inbox), output), threads=threads)
    if result.outcome is not ChannelEndOutcome.CLIENT_CLOSED:
        errors.write(result.channel_end().line().encode())
    return result.exit_status
