"""Run VibeSys turns on a launched ``agentshim.Session``.

Recovery policy (transient waits, a refused resume, a thread grown too large)
belongs to :class:`agentshim.Session`. This module is the VibeSys side of one
turn: choose between the provider's native response schema and the prompt
fallback, route the library's events to the turn's observer, classify what
went wrong into the errors callers catch, and report where the conversation
ended up. Type conversions live in :mod:`vs_agent.shim_translation`.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim

from vs_agent import shim_translation
from vs_agent.cli_common import build_schema_hint
from vs_agent.contracts import (
    DEFAULT_TURN_TIMEOUT_S,
    AgentOutputSchemaError,
    AgentTurnResult,
    AgentTurnTimeoutError,
    SteerOutcome,
)
from vs_agent.reply_limits import ReplyGuard, reply_breach
from vs_agent.session_errors import SessionResumeError
from vs_agent.step_tags import step_tag_env

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_agent.contracts import (
        AgentObserver,
        AgentSessionSpec,
        AgentTurnRequest,
        CapacityStops,
    )
    from vs_sim.api import Threads

_SCHEMA_DIR = Path(".cache/vibesys/response-schemas")
_TERMINAL_RESULT_ATTRIBUTE = "_vibesys_terminal_turn_result"


def terminal_result_from(error: BaseException) -> AgentTurnResult | None:
    """Return completed-turn evidence carried by an otherwise unchanged failure."""
    result = getattr(error, _TERMINAL_RESULT_ATTRIBUTE, None)
    return result if isinstance(result, AgentTurnResult) else None


def _with_terminal_result[ErrorT: BaseException](error: ErrorT, result: AgentTurnResult) -> ErrorT:
    """Carry typed terminal evidence without changing the public failure type."""
    setattr(error, _TERMINAL_RESULT_ATTRIBUTE, result)
    return error


class SteerLedger:
    """The steers a session offered whose fate the provider has not yet reported.

    A provider that accepts a message may still refuse it a moment later
    (``SteerRejected``). The ledger remembers each accepted text with the
    caller's fallback, so a refusal reaches the caller exactly once and a text
    the model consumed is forgotten. Thread-safe: ``steer`` runs on a
    caller's thread, the library's events on the turn's.
    """

    def __init__(self, threads: Threads) -> None:
        """Start with nothing outstanding; locks come from ``threads``."""
        self._lock = threads.lock()
        self._waiting: dict[str, deque[Callable[[], None]]] = {}
        self._refused: list[str] = []

    def expect(self, text: str, on_rejected: Callable[[], None]) -> None:
        """Remember an accepted *text* and the fallback for a later refusal."""
        with self._lock:
            self._waiting.setdefault(text, deque()).append(on_rejected)

    def forget(self, text: str, on_rejected: Callable[[], None]) -> None:
        """Drop an expectation the provider never accepted."""
        with self._lock:
            callbacks = self._waiting.get(text)
            if callbacks is not None and on_rejected in callbacks:
                callbacks.remove(on_rejected)
                if not callbacks:
                    del self._waiting[text]

    def consumed(self, text: str) -> None:
        """The model took *text* into the turn; it needs no fallback."""
        self._pop(text)

    def rejected(self, text: str) -> None:
        """The provider refused *text* after accepting it; run its fallback once."""
        callback = self._pop(text)
        if callback is not None:
            callback()

    def end_turn(self) -> None:
        """Resolve every steer still outstanding as refused, once, when a turn call ends.

        Whether the turn returned or raised (a quota stop, a dead process), a steer whose fate
        the provider never reported reaches its fallback here, so none is left unresolved.
        """
        with self._lock:
            waiting, self._waiting = self._waiting, {}
        for callbacks in waiting.values():
            for callback in callbacks:
                callback()

    def note_refused(self, steer_id: str) -> None:
        """Record a keyed steer the provider did not keep, for the turn's result."""
        with self._lock:
            self._refused.append(steer_id)

    def take_refused(self) -> tuple[str, ...]:
        """The keyed steers refused since the last call, and forget them."""
        with self._lock:
            refused, self._refused = self._refused, []
        return tuple(refused)

    def _pop(self, text: str) -> Callable[[], None] | None:
        with self._lock:
            callbacks = self._waiting.get(text)
            if not callbacks:
                return None
            callback = callbacks.popleft()
            if not callbacks:
                del self._waiting[text]
            return callback


class TurnEvents:
    """The ``agentshim.AgentEventHandler`` of one session: events to the turn's observer.

    One instance is registered with the ``Agent`` that makes the session, and
    the turn in flight points it at its observer.

    The library calls :meth:`on_event` on its own thread while a caller may :meth:`cancel`
    from another (a real thread, even under simulated threads, because the library's
    interrupt blocks), so the two must agree which of them stops a running turn. Each turn
    has one delivery token; stopping the running turn takes it, and only the side that took
    it stops the turn. Taking it is one ``list.pop``, atomic on its own, so no lock is shared
    with the caller's thread.
    """

    def __init__(self, steers: SteerLedger) -> None:
        """Start with no turn in flight."""
        self.observer: AgentObserver | None = None
        #: Whether the turn in flight asked for a response schema.
        self.structured = False
        #: Windows the provider reported exhausted during the turn in flight.
        self.exhausted: list[agentshim.RateLimitStatus] = []
        #: Why the turn in flight was stopped for its reply, or ``None``.
        self.breach: str | None = None
        #: Whether the caller cancelled the turn in flight.
        self.cancelled = False
        self._guard = ReplyGuard()
        self._stop: Callable[[], None] | None = None
        self._turn: CapacityStops | None = None
        self._delivery: list[None] = []
        self._running = False
        self._steers = steers

    def begin_turn(
        self, *, structured: bool, stop: Callable[[], None], turn: CapacityStops | None
    ) -> None:
        """Start watching a turn; *stop* ends it when its reply crosses a limit.

        ``turn`` is the caller's record of the turn, when it keeps one. A cancellation
        it already holds stays in force, and one that lands before the library runs the
        turn (when an interrupt has nothing to stop) stops it at its first event.
        """
        self.structured = structured
        self.exhausted = []
        self.breach = None
        self.cancelled = turn is not None and turn.cancelled
        self._guard = ReplyGuard()
        self._stop = stop
        self._turn = turn
        self._delivery = [None]
        self._running = False

    def cancel(self, interrupt: Callable[[], None], turn: CapacityStops | None = None) -> None:
        """Cancel ``turn`` (the caller's record, when it keeps one) and stop it with *interrupt*.

        Once the library has run the turn (it emitted an event), *interrupt* reaches it, so
        the turn's next event does not stop it again. Before that, an interrupt has nothing
        to stop, and the turn's first event delivers the cancellation instead. When an
        event racing this cancel already stopped the running turn, the cancel does not stop
        it again.
        """
        if turn is not None:
            turn.cancel()
        self.cancelled = True
        # ``_running`` is read after ``turn`` is marked, and set before an event reads the
        # mark, so a running turn that missed the mark is stopped here, and one that saw
        # it raced this cancel for the token.
        if self._running and not self._take_delivery():
            return
        interrupt()

    def on_event(self, event: agentshim.AgentEvent) -> None:
        """Translate and forward one library event, if anyone is listening."""
        self._running = True
        self._stop_if_cancelled()
        if isinstance(event, agentshim.SteerConsumed):
            self._steers.consumed(event.text)
        elif isinstance(event, agentshim.SteerRejected):
            self._steers.rejected(event.text)
        elif isinstance(event, agentshim.RateLimitStatus) and event.exhausted:
            self.exhausted.append(event)
        elif isinstance(event, agentshim.AssistantText) and self._refuse_reply(event.text):
            # The refused text is not forwarded: it is the flood being refused.
            return
        observer = self.observer
        if observer is None:
            return
        translated = shim_translation.event_from(event, structured=self.structured)
        if translated is not None:
            observer.on_event(translated)

    def _stop_if_cancelled(self) -> None:
        """Stop the turn once if its caller cancelled it and no interrupt reached it yet."""
        turn = self._turn
        if turn is None or not turn.cancelled or not self._take_delivery():
            return
        self.cancelled = True
        if self._stop is not None:
            self._stop()

    def _take_delivery(self) -> bool:
        """Take this turn's one delivery token; False when the other side already took it."""
        try:
            self._delivery.pop()
        except IndexError:
            return False
        return True

    def _refuse_reply(self, text: str) -> bool:
        """Stop the turn the first time its reply crosses a limit; whether *text* is refused."""
        if self.breach is None:
            self.breach = self._guard.feed(text)
            if self.breach is not None and self._stop is not None:
                self._stop()
        return self.breach is not None


class LaunchedSession:
    """One ``agentshim.Session`` with the VibeSys facts its turns need from its launch.

    ``agent_path`` maps a host path to the path the agent process sees (the
    identity where nothing is confined). ``steers`` and ``events`` are the
    handler state shared with the ``Agent`` that made ``session``.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-010320 [PLR0913]; a launched session is exactly these independent facts of one launch, passed by name.
        self,
        *,
        session: agentshim.Session,
        profile: agentshim.ProviderProfile,
        spec: AgentSessionSpec,
        events: TurnEvents,
        steers: SteerLedger,
        agent_path: Callable[[str], str],
        timeout: int | None,
        log: Callable[[str], None],
    ) -> None:
        """Bind a library session to the launch facts its turns need."""
        self.session = session
        self.profile = profile
        self.spec = spec
        self.events = events
        self.steers = steers
        self.agent_path = agent_path
        self.timeout = timeout
        self.log = log
        self.closed = False
        self.context_tokens: int | None = None
        """The tokens the conversation's context held after its last completed turn.

        Read from agentshim's ``TurnResult.context_tokens``: the last model call's prompt plus
        its output, not the turn's summed usage. ``None`` when the provider did not say.
        """
        self._turn: CapacityStops | None = None

    def track(self, turn: CapacityStops | None) -> None:
        """Make :meth:`cancel` reach ``turn``, the caller's record of the turn in flight."""
        self._turn = turn

    @property
    def tracked(self) -> CapacityStops | None:
        """The caller's record of the turn in flight, when it keeps one."""
        return self._turn

    def close(self) -> None:
        """Release this logical session, stopping any turn it still owns."""
        self.session.close()
        self.closed = True
        self.events.observer = None

    def cancel(self) -> None:
        """Stop an in-flight turn by terminating the provider process.

        A turn waiting out a transient provider error stops waiting and raises
        that error instead of retrying, as a plain failure: a cancelled turn is
        never a capacity stop for the caller to wait out. A turn the caller's
        capacity gate is waiting on is cancelled through its tracked record,
        which ends that wait and keeps the turn from being sent again.
        """
        self.events.cancel(self.session.interrupt, self._turn)

    def adopt(self, session_id: str) -> bool:
        """Continue ``session_id`` on the next turn, if the session accepts it.

        A session that already named a conversation keeps it: its history is
        newer than any checkpoint the caller holds. Beyond that the library
        decides, adopting the ID only when the provider has a resume flag and
        no turn is in flight. A stale or deleted transcript is handled later,
        by the library's fresh-conversation retry around the turn.
        """
        return self.session.adopt(session_id)

    def run_turn(
        self, request: AgentTurnRequest, observer: AgentObserver | None = None
    ) -> AgentTurnResult:
        """Add one turn to the conversation and return its raw result.

        Raises :class:`~vs_agent.contracts.AgentTurnTimeoutError` when the turn
        outlives its budget, ``AgentOutputSchemaError`` when the provider gave
        up matching the response schema (the conversation is kept for a
        correction), and ``AgentQuotaError`` on a capacity limit.
        """
        return _run_turn(self, request, observer)

    def steer(self, text: str, *, on_rejected: Callable[[], None]) -> SteerOutcome:
        """Offer *text* to the running turn, on a transport that can take it.

        A one-shot provider process reads no input after launch, so its
        profile says it cannot steer and the library is not asked. A refusal
        the provider reports after accepting the message calls *on_rejected*,
        once, on the thread running the turn, and so does a message still
        unresolved when the turn call returns or raises; it is never called for
        any other outcome, and never after the turn call ended.
        """
        return _steer_turn(self, text, on_rejected=on_rejected)


def _run_turn(
    live: LaunchedSession,
    request: AgentTurnRequest,
    observer: AgentObserver | None = None,
) -> AgentTurnResult:
    """Add one turn to the conversation and return its raw result."""
    if live.closed:
        message = "agent session is closed"
        raise RuntimeError(message)

    turn_record = live.tracked
    if turn_record is not None and turn_record.cancelled:
        # Cancelled since the caller last decided to send it, when an interrupt had no
        # turn to stop: it is not sent. A cancel landing after this check is seen by
        # ``begin_turn`` or, once the library runs the turn, at its first event.
        message = "turn cancelled before it was sent"
        raise agentshim.TurnCancelledError(message)

    expected = request.expected_provider_session_id
    events = live.events
    events.observer = observer
    events.begin_turn(
        structured=request.output_schema is not None,
        stop=live.session.interrupt,
        turn=turn_record,
    )
    try:
        if expected is None:
            turn = _run(live, request, expect_conversation=None)
        else:
            try:
                turn = _run(live, request, expect_conversation=expected)
            except agentshim.ContinuityError as error:
                raise SessionResumeError(
                    expected, "session has not adopted the expected conversation"
                ) from error
            except agentshim.AgentShimError as error:
                raise SessionResumeError(expected, str(error)) from error
    except Exception as error:
        # A turn stopped for its reply fails however the library reports the stop.
        if events.breach is not None:
            usage = error.usage if isinstance(error, AgentTurnTimeoutError) else None
            raise AgentOutputSchemaError(events.breach, usage=usage) from error
        raise
    finally:
        events.observer = None
        events.structured = False
        live.steers.end_turn()

    result = turn.result
    live.context_tokens = result.context_tokens
    usage = shim_translation.usage_from(
        result.usage,
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )
    normalized = AgentTurnResult(
        text=shim_translation.result_text(result),
        usage=usage,
        provider_session_id=result.session_id,
        restarted=turn.continuity is not agentshim.Continuity.CONTINUED,
        skills=shim_translation.skill_use_from(result.skills),
        refused_steers=live.steers.take_refused(),
    )
    refusal = events.breach or reply_breach(shim_translation.result_text(result))
    if refusal is not None:
        raise _with_terminal_result(AgentOutputSchemaError(refusal, usage=usage), normalized)
    if result.interrupted:
        # The conversation survives an interrupt, but the caller asked for
        # an answer and there is none: a stopped turn raises.
        message = "turn interrupted"
        # Keep the exact public exception type/catch contract while carrying
        # evidence that was terminal before cancellation reached the caller.
        raise _with_terminal_result(agentshim.TurnCancelledError(message), normalized)
    # RESET and REPLACED both say the conversation the caller was
    # continuing is gone (renewed, or restarted after a refused resume).
    _log_continuity(live, turn.continuity)
    return normalized


def _steer_turn(
    live: LaunchedSession, text: str, *, on_rejected: Callable[[], None]
) -> SteerOutcome:
    """Offer *text* to the running turn; see :meth:`LaunchedSession.steer`."""
    if not live.profile.supports_steer:
        return SteerOutcome.UNSUPPORTED
    live.steers.expect(text, on_rejected)
    try:
        live.session.steer(text)
    except agentshim.NoRunningTurnError:
        live.steers.forget(text, on_rejected)
        return SteerOutcome.NO_RUNNING_TURN
    except agentshim.ProviderCapabilityError:
        live.steers.forget(text, on_rejected)
        return SteerOutcome.UNSUPPORTED
    return SteerOutcome.DELIVERED


def _log_continuity(live: LaunchedSession, continuity: agentshim.Continuity) -> None:
    """Tell the operator when the library dropped the conversation behind a turn.

    The library decides these restarts silently, so this is the only record
    that history was lost.
    """
    name = live.profile.name
    if continuity is agentshim.Continuity.RESET:
        live.log(f"renewing {name} thread; durable workspace state remains authoritative.")
    elif continuity is agentshim.Continuity.REPLACED:
        live.log(f"{name} session is no longer available; this turn ran in a fresh conversation.")


def _run(
    live: LaunchedSession, request: AgentTurnRequest, *, expect_conversation: str | None
) -> agentshim.Turn:
    """Prepare and run one turn, translating library failures to VibeSys errors.

    A provider that gave up matching the output schema
    (``agentshim.FailureKind.SCHEMA``) raises ``AgentOutputSchemaError``
    with its validation errors. The library has already decided whether to
    retry by then, and keeps the conversation the correction turn continues.
    """
    session = live.session
    held = session.conversation_id
    ticket = session.prepare_turn(
        _turn_request(live, request),
        expect_conversation=expect_conversation,
        pin=request.require_provider_checkpoint,
    )
    try:
        return session.run(ticket)
    except (OSError, ImportError, agentshim.CliNotFoundError) as exc:
        raise shim_translation.spawn_error_from(live.profile.name, exc) from exc
    except agentshim.TurnTimeoutError as exc:
        raise shim_translation.timeout_error_from(exc) from exc
    except agentshim.TurnFailedError as exc:
        if held is not None and session.conversation_id is None:
            live.log(
                f"the resumed {live.profile.name} turn failed; dropped the conversation "
                "so the next turn starts fresh."
            )
        classified = shim_translation.failure_from(
            exc, provider=live.profile.name, exhausted=live.events.exhausted
        )
        if classified is None:
            raise
        # A cancelled turn keeps its capacity type (S179): the caller decides what a
        # cancel means, and the client never sends a cancelled turn to its gate.
        raise classified from exc


def _turn_request(live: LaunchedSession, request: AgentTurnRequest) -> agentshim.TurnRequest:
    """Translate one VibeSys turn into the library's request."""
    schema, schema_hint = _output_schema(live, request.output_schema)
    timeout = DEFAULT_TURN_TIMEOUT_S if live.timeout is None else live.timeout
    if request.timeout is not None:
        timeout = max(1, int(request.timeout.total_seconds()))
    return agentshim.TurnRequest(
        prompt=f"{request.instructions}\n\n{request.message}{schema_hint}",
        timeout=timeout,
        output_schema=schema,
        reasoning_effort=(
            live.spec.reasoning_effort if live.profile.supports_reasoning_effort else None
        ),
        env=None if request.step_tag is None else step_tag_env(request.step_tag),
    )


def _output_schema(
    live: LaunchedSession,
    response_cls: type[BaseModel] | None,
) -> tuple[agentshim.OutputSchema | None, str]:
    """Choose between the provider's native schema and the prompt contract.

    The prompt-level instruction is the portable fallback: it is used when
    the provider has no structured-output flag, and when the response model
    needs JSON Schema constructs the provider's dialect rejects. Falling
    back is logged because the two paths fail differently.
    """
    if response_cls is None:
        return None, ""
    profile = live.profile
    if profile.output_schema is agentshim.OutputSchemaStyle.NONE:
        live.log(
            f"[structured-output] {profile.name} has no native output schema for "
            f"{response_cls.__name__}; using prompt fallback"
        )
        return None, build_schema_hint(response_cls)

    schema, problems = _native_schema(response_cls, profile)
    if problems:
        live.log(
            f"[structured-output] native schema unavailable for "
            f"{response_cls.__name__}; using prompt fallback: {'; '.join(problems)}"
        )
        return None, build_schema_hint(response_cls)

    host_dir = live.spec.workspace / _SCHEMA_DIR
    return (
        agentshim.OutputSchema(
            schema=schema, host_dir=host_dir, cli_dir=live.agent_path(str(host_dir))
        ),
        "",
    )


def _native_schema(
    response_cls: type[BaseModel], profile: agentshim.ProviderProfile
) -> tuple[dict[str, object], list[str]]:
    """Return the schema the CLI would receive for *response_cls*, and what its dialect rejects.

    The dialect check runs on the schema the CLI will receive: the normalizer is
    what makes pydantic's optional fields nullable and closes objects, so
    checking the raw schema rejects models the provider accepts.
    """
    dialect = (
        profile.schema_dialect
        if profile.schema_dialect is not None
        else agentshim.SchemaDialect.STRICT
    )
    schema = agentshim.normalize(response_cls.model_json_schema(), dialect)
    return schema, agentshim.dialect_problems(schema, dialect)


def native_schema_problems(response_cls: type[BaseModel], provider: str) -> list[str]:
    """List why *provider*'s native response-schema dialect rejects *response_cls*.

    An empty list means the provider receives the model as a native schema; a
    non-empty one means a turn falls back to the prompt-level contract.
    """
    return _native_schema(response_cls, agentshim.get_provider(provider).profile)[1]


def native_schema(response_cls: type[BaseModel], provider: str) -> dict[str, object]:
    """The JSON Schema *provider* receives for *response_cls* when it takes a native schema.

    This is the schema after the provider's dialect rewrite, whether or not that dialect
    accepts the model (see ``native_schema_problems``).
    """
    return _native_schema(response_cls, agentshim.get_provider(provider).profile)[0]
