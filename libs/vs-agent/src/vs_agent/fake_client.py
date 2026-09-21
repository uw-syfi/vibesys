"""In-memory, configurable :class:`AgentClientProtocol` test double.

Where :class:`~vs_agent.stub_runner.StubAgentClient` returns the same scripted
rounds with no configuration, ``FakeAgentClient`` lets a test assert what a
caller actually sent (prompts, MCP servers, session keys), inject specific or
failing responses, and observe streamed output and session-reuse behavior.
Zero-config it behaves like the stub (scripted structured responses, a fixed
default text); every call is recorded as a :class:`FakeInvocation` for direct
assertions.

This module stays schema-agnostic (no ``vibesys`` core imports) and driver-
agnostic (no ``agentshim``/``omnigent`` imports): callers enqueue already-
constructed response objects, and this module never runs an external agent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Literal, Self, TypeVar

from pydantic import BaseModel

from vs_agent.contracts import AgentCapabilities, MCPServerSpec
from vs_agent.progress import AgentProgress  # noqa: TC001  # tracked: #288
from vs_agent.scripted_rounds import round_number_from_label, scripted_round_payload
from vs_agent.session_key import AgentSessionKey  # noqa: TC001  # tracked: #288
from vs_agent.sink import NULL_AGENT_EVENT_SINK, AgentEventSink

T = TypeVar("T", bound=BaseModel)

#: Default answer :meth:`FakeAgentClient.invoke_text` returns when nothing is
#: configured for the call's ``kind`` and no module-wide default was set.
DEFAULT_TEXT = "Fake agent inspected the trajectory."

type ResponseValue = BaseModel | dict[str, object]
type ResponseSource = ResponseValue | Callable[[FakeInvocation], ResponseValue]
type TextSource = str | Callable[[FakeInvocation], str]


class _ParseFailure:
    """Queue marker for a turn that returns unparseable output.

    When one is dequeued, the loop falls back to ``fallback_factory()`` (a
    synthesized response) instead of a parsed model.
    """


#: Singleton enqueued by :meth:`FakeAgentClient.enqueue_parse_failure`.
_PARSE_FAILURE = _ParseFailure()

type QueuedResponse = ResponseSource | _ParseFailure


@dataclass(frozen=True, slots=True)
class FakeInvocation:
    """One recorded call to :meth:`FakeAgentClient.invoke` or ``invoke_text``."""

    method: Literal["invoke", "invoke_text"]
    kind: str
    workspace: Path
    system_prompt: str
    user_prompt: str
    round_label: str
    response_cls: type[BaseModel] | None
    env: dict[str, str] | None
    invocation_id: str | None
    progress: AgentProgress | None
    mcp_servers: list[MCPServerSpec] | None
    reuse_session: bool | None
    session_key: AgentSessionKey | None


@dataclass(slots=True)
class _FailureState:
    """Mutable countdown for a configured :meth:`FakeAgentClient.fail`."""

    exc: BaseException
    #: ``None`` means "raise forever"; otherwise the number of remaining calls
    #: that still raise before the client resumes normal responses.
    remaining: int | None


def _materialize_response(source: ResponseSource, invocation: FakeInvocation) -> ResponseValue:
    """Resolve a configured structured-response source, calling it if callable."""
    if isinstance(source, BaseModel | dict):
        return source
    return source(invocation)


def _materialize_text(source: TextSource, invocation: FakeInvocation) -> str:
    """Resolve a configured text source, calling it if callable."""
    if isinstance(source, str):
        return source
    return source(invocation)


_PopT = TypeVar("_PopT")


def _pop(queues: dict[str, list[_PopT]], kind: str) -> _PopT | None:
    """Pop and return ``kind``'s next queued item, or ``None`` if empty/absent."""
    queue = queues.get(kind)
    if not queue:
        return None
    return queue.pop(0)


class FakeAgentClient:
    """Configurable in-memory double for :class:`~vs_agent.contracts.AgentClientProtocol`.

    Zero-config it behaves like :class:`~vs_agent.stub_runner.StubAgentClient`:
    ``invoke`` returns a scripted round payload (or ``fallback_factory()`` when
    the response model is unscripted) and ``invoke_text`` returns a fixed
    default sentence. Configured through the chained ``enqueue``/``set_*``/
    ``fail``/``on_invoke`` methods, it can return specific responses per agent
    ``kind``, fail on demand, stream output through the injected event sink,
    and track provider-session reuse.
    """

    backend_name = "fake"

    def __init__(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        backend_name: str = "fake",
        driver_name: str | None = "fake",
        provider: str | None = "fake",
        model: str | None = None,
        session_reuse: bool = False,
        capabilities: AgentCapabilities | None = None,
        event_sink: AgentEventSink = NULL_AGENT_EVENT_SINK,
    ) -> None:
        """Create a fake client; see the class docstring for defaults.

        ``capabilities`` overrides the reported feature set (e.g. to report
        ``mcp_servers=True`` for a backend that hosts issue-board tools); when
        omitted it is ``AgentCapabilities(session_reuse=session_reuse)``.
        """
        # Instance attribute shadows the class default so a test can report a
        # different backend (e.g. "cli") without subclassing.
        self.backend_name = backend_name
        self._driver_name = driver_name
        self._provider = provider
        self._model = model
        self._capabilities = (
            capabilities
            if capabilities is not None
            else AgentCapabilities(session_reuse=session_reuse)
        )
        self._session_reuse = self._capabilities.session_reuse
        self._sink = event_sink
        self._default_text: TextSource = DEFAULT_TEXT

        self.calls: list[FakeInvocation] = []
        #: Streams passed to :meth:`set_log_file`, in call order, so a test can
        #: assert the run logger was wired to the client.
        self.log_files: list[object] = []

        self._queues: dict[str, list[QueuedResponse]] = {}
        self._constants: dict[str, ResponseSource] = {}
        self._text_queues: dict[str, list[TextSource]] = {}
        self._text_constants: dict[str, TextSource] = {}
        self._failures: dict[str, _FailureState] = {}
        self._model_for_kind: dict[str, str] = {}
        self._stream_chunks: dict[str, list[str]] = {}
        self._on_invoke_callbacks: list[Callable[[FakeInvocation], None]] = []

        self._sessions: dict[AgentSessionKey, str] = {}
        self._last_turn_sessions: dict[AgentSessionKey, str] = {}
        self._session_counter = 0

        self._closed = False

    # -- AgentClientProtocol: attribution ---------------------------------

    @property
    def capabilities(self) -> AgentCapabilities:
        """Report the configured capability set (see ``capabilities`` ctor arg)."""
        return self._capabilities

    @property
    def driver_name(self) -> str | None:
        """Return the configured driver name (default ``"fake"``)."""
        return self._driver_name

    @property
    def provider(self) -> str | None:
        """Return the configured provider name (default ``"fake"``)."""
        return self._provider

    def model_for_kind(self, kind: str) -> str | None:
        """Return the per-kind model override, falling back to the ctor model."""
        return self._model_for_kind.get(kind, self._model)

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Return the id minted or seeded for ``session_key``, else ``None``."""
        return self._sessions.get(session_key)

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Return the id used on ``session_key``'s last turn, else ``None``."""
        return self._last_turn_sessions.get(session_key)

    def set_log_file(self, stream: object) -> None:
        """Record the retarget stream (in ``log_files``); emit no file logs."""
        self.log_files.append(stream)

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called (observable teardown)."""
        return self._closed

    def close(self) -> None:
        """Mark this client closed. Idempotent, like every ``close()``."""
        self._closed = True

    # -- configuration (each returns self for chaining) --------------------

    def enqueue(self, kind: str, *responses: ResponseSource) -> Self:
        """Queue structured responses ``invoke(kind=kind, ...)`` pops in order."""
        self._queues.setdefault(kind, []).extend(responses)
        return self

    def set_response(self, kind: str, response: ResponseSource) -> Self:
        """Set the constant structured response used once ``kind``'s queue empties."""
        self._constants[kind] = response
        return self

    def enqueue_text(self, kind: str, *texts: TextSource) -> Self:
        """Queue plain-text responses ``invoke_text(kind=kind, ...)`` pops in order."""
        self._text_queues.setdefault(kind, []).extend(texts)
        return self

    def set_text(self, kind: str | None, text: TextSource) -> Self:
        """Set the constant text for ``kind``, or the module-wide default if ``None``."""
        if kind is None:
            self._default_text = text
        else:
            self._text_constants[kind] = text
        return self

    def fail(self, kind: str, exc: BaseException, *, times: int | None = None) -> Self:
        """Raise ``exc`` from ``invoke``/``invoke_text`` calls of ``kind``.

        ``exc`` may be any ``BaseException`` so a test can simulate cancellation
        (``KeyboardInterrupt``) as faithfully as an ordinary error. ``times=None``
        fails forever; otherwise the next ``times`` calls fail and the client
        then resumes normal responses.
        """
        self._failures[kind] = _FailureState(exc=exc, remaining=times)
        return self

    def enqueue_parse_failure(self, kind: str, *, count: int = 1) -> Self:
        """Queue ``count`` parse failures for ``invoke(kind=...)``, in call order.

        Models the real client emitting output the loop cannot parse into
        ``response_cls``: for that turn the loop falls back to
        ``fallback_factory()`` (a synthesized response). Queued positionally
        alongside :meth:`enqueue`, so ``enqueue(kind, ok).enqueue_parse_failure(kind)``
        makes the first turn succeed and the second parse-fail. The call is
        still recorded. Only affects :meth:`invoke`, not :meth:`invoke_text`.
        """
        self._queues.setdefault(kind, []).extend([_PARSE_FAILURE] * count)
        return self

    def set_attribution(
        self,
        *,
        driver_name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> Self:
        """Override attribution props; only the given (non-``None``) fields change."""
        if driver_name is not None:
            self._driver_name = driver_name
        if provider is not None:
            self._provider = provider
        if model is not None:
            self._model = model
        return self

    def set_model_for_kind(self, mapping: dict[str, str]) -> Self:
        """Set per-kind model overrides, merged into any already configured."""
        self._model_for_kind.update(mapping)
        return self

    def set_capabilities(self, capabilities: AgentCapabilities) -> Self:
        """Replace the reported capability set (e.g. to enable ``mcp_servers``)."""
        self._capabilities = capabilities
        self._session_reuse = capabilities.session_reuse
        return self

    def set_session(
        self,
        session_key: AgentSessionKey,
        *,
        provider_session_id: str | None = None,
        last_turn: str | None = None,
    ) -> Self:
        """Seed session ids for ``session_key`` without an invoke minting them."""
        if provider_session_id is not None:
            self._sessions[session_key] = provider_session_id
        if last_turn is not None:
            self._last_turn_sessions[session_key] = last_turn
        return self

    def evict_session(self, session_key: AgentSessionKey) -> Self:
        """Forget ``session_key``'s ids, so ``provider_session_id`` reports ``None``.

        Models a provider conversation retiring: after this the next
        ``reuse_session`` turn mints a fresh id instead of continuing.
        """
        self._sessions.pop(session_key, None)
        self._last_turn_sessions.pop(session_key, None)
        return self

    def stream_output(self, kind: str, chunks: Sequence[str]) -> Self:
        """Emit ``chunks`` through the event sink on every future call of ``kind``."""
        self._stream_chunks[kind] = list(chunks)
        return self

    def on_invoke(self, callback: Callable[[FakeInvocation], None]) -> Self:
        """Register ``callback`` to run, in registration order, on every call."""
        self._on_invoke_callbacks.append(callback)
        return self

    # -- FakeInvocation lookup ----------------------------------------------

    def calls_for(self, kind: str) -> list[FakeInvocation]:
        """Return recorded calls whose ``kind`` matches, in call order."""
        return [call for call in self.calls if call.kind == kind]

    # -- AgentClientProtocol: turns ------------------------------------------

    def invoke(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> T:
        """Run one structured turn against the configured/scripted response."""
        invocation = self._record(
            method="invoke",
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            round_label=round_label,
            response_cls=response_cls,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        self._maybe_raise(kind)
        self._update_session(reuse_session=reuse_session, session_key=session_key)
        self._emit_stream(kind, invocation)
        return self._resolve_response(kind, invocation, response_cls, fallback_factory)

    def invoke_text(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Run one conversational turn against the configured/default text."""
        invocation = self._record(
            method="invoke_text",
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            round_label=round_label,
            response_cls=None,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        self._maybe_raise(kind)
        self._update_session(reuse_session=reuse_session, session_key=session_key)
        self._emit_stream(kind, invocation)
        return self._resolve_text(kind, invocation)

    # -- internals ------------------------------------------------------------

    def _record(  # noqa: PLR0913
        self,
        *,
        method: Literal["invoke", "invoke_text"],
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        round_label: str,
        response_cls: type[BaseModel] | None,
        env: dict[str, str] | None,
        invocation_id: str | None,
        progress: AgentProgress | None,
        mcp_servers: list[MCPServerSpec] | None,
        reuse_session: bool | None,
        session_key: AgentSessionKey | None,
    ) -> FakeInvocation:
        invocation = FakeInvocation(
            method=method,
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            round_label=round_label,
            response_cls=response_cls,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            mcp_servers=mcp_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        self.calls.append(invocation)
        for callback in self._on_invoke_callbacks:
            callback(invocation)
        return invocation

    def _maybe_raise(self, kind: str) -> None:
        state = self._failures.get(kind)
        if state is None:
            return
        if state.remaining is None:
            raise state.exc
        if state.remaining > 0:
            state.remaining -= 1
            raise state.exc

    def _update_session(
        self, *, reuse_session: bool | None, session_key: AgentSessionKey | None
    ) -> None:
        if not (self._session_reuse and reuse_session and session_key is not None):
            return
        session_id = self._sessions.get(session_key)
        if session_id is None:
            self._session_counter += 1
            session_id = f"fake-session-{self._session_counter}"
            self._sessions[session_key] = session_id
        self._last_turn_sessions[session_key] = session_id

    def _emit_stream(self, kind: str, invocation: FakeInvocation) -> None:
        chunks = self._stream_chunks.get(kind)
        if not chunks:
            return
        for chunk in chunks:
            self._sink.agent_output(
                chunk,
                channel="assistant",
                agent_kind=kind,
                round_label=invocation.round_label,
                invocation_id=invocation.invocation_id,
            )

    def _resolve_response(
        self,
        kind: str,
        invocation: FakeInvocation,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
    ) -> T:
        source = _pop(self._queues, kind)
        if isinstance(source, _ParseFailure):
            return fallback_factory()
        if source is None:
            source = self._constants.get(kind)
        if source is None:
            scripted = scripted_round_payload(
                response_cls.__name__, round_number_from_label(invocation.round_label)
            )
            if scripted is None:
                return fallback_factory()
            return response_cls.model_validate(scripted)
        value = _materialize_response(source, invocation)
        if isinstance(value, BaseModel):
            # A model instance is returned as-is; the caller enqueued it (rather
            # than a dict) and owns it matching ``response_cls``.
            return value  # ty: ignore[invalid-return-type]  # tracked: #288
        return response_cls.model_validate(value)

    def _resolve_text(self, kind: str, invocation: FakeInvocation) -> str:
        source = _pop(self._text_queues, kind)
        if source is None:
            source = self._text_constants.get(kind, self._default_text)
        return _materialize_text(source, invocation)
