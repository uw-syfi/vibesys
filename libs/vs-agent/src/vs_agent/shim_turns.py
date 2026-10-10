"""Run VibeSys turns on a launched ``agentshim.Session``.

Recovery policy (transient waits, a refused resume, a thread grown too large)
belongs to :class:`agentshim.Session`. This module is the VibeSys side of one
turn: choose between the provider's native response schema and the prompt
fallback, route the library's events to the turn's observer, classify what
went wrong into the errors callers catch, and report where the conversation
ended up. Type conversions live in :mod:`vs_agent.shim_translation`.
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim

from vs_agent import shim_translation
from vs_agent.cli_common import build_schema_hint
from vs_agent.contracts import AgentTurnResult, SteerOutcome
from vs_agent.session_errors import SessionResumeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_agent.contracts import (
        AgentObserver,
        AgentSessionSpec,
        AgentTurnRequest,
    )

_SCHEMA_DIR = Path(".cache/vibesys/response-schemas")


class SteerLedger:
    """The steers a session offered whose fate the provider has not yet reported.

    A provider that accepts a message may still refuse it a moment later
    (``SteerRejected``). The ledger remembers each accepted text with the
    caller's fallback, so a refusal reaches the caller exactly once and a text
    the model consumed is forgotten. Thread-safe: ``steer`` runs on a
    caller's thread, the library's events on the turn's.
    """

    def __init__(self) -> None:
        """Start with nothing outstanding."""
        self._lock = threading.Lock()
        self._waiting: dict[str, deque[Callable[[], None]]] = {}

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

    def clear(self) -> None:
        """Forget everything outstanding at a turn boundary."""
        with self._lock:
            self._waiting.clear()

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
    """

    def __init__(self, steers: SteerLedger) -> None:
        """Start with no turn in flight."""
        self.observer: AgentObserver | None = None
        #: Whether the turn in flight asked for a response schema.
        self.structured = False
        #: Windows the provider reported exhausted during the turn in flight.
        self.exhausted: list[agentshim.RateLimitStatus] = []
        self._steers = steers

    def on_event(self, event: agentshim.AgentEvent) -> None:
        """Translate and forward one library event, if anyone is listening."""
        if isinstance(event, agentshim.SteerConsumed):
            self._steers.consumed(event.text)
        elif isinstance(event, agentshim.SteerRejected):
            self._steers.rejected(event.text)
        elif isinstance(event, agentshim.RateLimitStatus) and event.exhausted:
            self.exhausted.append(event)
        observer = self.observer
        if observer is None:
            return
        translated = shim_translation.event_from(event, structured=self.structured)
        if translated is not None:
            observer.on_event(translated)


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
        mcp_servers: tuple[agentshim.StdioMcpServer, ...] = (),
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
        self.mcp_servers = mcp_servers
        self.closed = False

    def close(self) -> None:
        """Release this logical session, stopping any turn it still owns."""
        self.session.close()
        self.closed = True
        self.events.observer = None

    def cancel(self) -> None:
        """Stop an in-flight turn by terminating the provider process.

        A turn waiting out a transient provider error stops waiting and raises
        that error instead of retrying.
        """
        self.session.interrupt()

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
        once, on the thread running the turn; it is never called for any other
        outcome.
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

    expected = request.expected_provider_session_id
    events = live.events
    events.observer = observer
    events.structured = request.output_schema is not None
    events.exhausted = []
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
    finally:
        events.observer = None
        events.structured = False
        live.steers.clear()

    result = turn.result
    if result.interrupted:
        # The conversation survives an interrupt, but the caller asked for
        # an answer and there is none: a stopped turn raises.
        message = "turn interrupted"
        raise agentshim.TurnCancelledError(message)
    # RESET and REPLACED both say the conversation the caller was
    # continuing is gone (renewed, or restarted after a refused resume).
    _log_continuity(live, turn.continuity)
    return AgentTurnResult(
        text=shim_translation.result_text(result),
        usage=shim_translation.usage_from(
            result.usage,
            cost_usd=result.cost_usd,
            duration_ms=result.duration_ms,
        ),
        provider_session_id=result.session_id,
        restarted=turn.continuity is not agentshim.Continuity.CONTINUED,
        skills=shim_translation.skill_use_from(result.skills),
    )


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
        classified = shim_translation.failure_from(
            exc, provider=live.profile.name, exhausted=live.events.exhausted
        )
        if classified is not None:
            raise classified from exc
        if held is not None and session.conversation_id is None:
            live.log(
                f"the resumed {live.profile.name} turn failed; dropped the conversation "
                "so the next turn starts fresh."
            )
        raise


def _turn_request(live: LaunchedSession, request: AgentTurnRequest) -> agentshim.TurnRequest:
    """Translate one VibeSys turn into the library's request."""
    schema, schema_hint = _output_schema(live, request.output_schema)
    timeout = live.timeout
    if request.timeout is not None:
        timeout = max(1, int(request.timeout.total_seconds()))
    return agentshim.TurnRequest(
        prompt=f"{request.instructions}\n\n{request.message}{schema_hint}",
        timeout=timeout,
        output_schema=schema,
        reasoning_effort=(
            live.spec.reasoning_effort if live.profile.supports_reasoning_effort else None
        ),
        mcp_servers=live.mcp_servers,
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
