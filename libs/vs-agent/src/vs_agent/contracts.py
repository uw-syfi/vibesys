"""Implementation-neutral contracts for stateful agent execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta
    from pathlib import Path
    from typing import TextIO

    from vs_agent.progress import AgentProgress
    from vs_agent.session_key import AgentSessionKey
    from vs_mcp.api import ToolServerDescriptor
    from vs_sandbox.api import HostResource, ProjectPathPolicy
    from vs_sim.api import Threads

T = TypeVar("T", bound=BaseModel)


class AgentSpawnError(RuntimeError):
    """The agent process could not start; a subsequent turn may retry setup.

    No agent work has succeeded. The provider and original diagnostic identify
    the failed external boundary without exposing a provider exception type.
    """

    retryable = True

    def __init__(self, provider: str, detail: str) -> None:
        """Record the provider and preserve the startup failure diagnostic."""
        self.provider = provider
        self.detail = detail
        super().__init__(f"could not start {provider} agent: {detail}")


class AgentTurnTimeoutError(TimeoutError):
    """An agent turn exceeded its configured wall-clock budget.

    ``usage`` is the accounting from a partial turn parsed before the timeout,
    when one reached the boundary. It stays ``None`` when there was no parsed
    partial turn; individual fields retain the provider's known/unknown semantics.
    """

    def __init__(self, timeout_seconds: float, *, usage: AgentUsage | None = None) -> None:
        """Record the configured budget and any provider-reported partial usage."""
        self.timeout_seconds = timeout_seconds
        self.usage = usage
        super().__init__(f"agent turn timed out after {timeout_seconds:g} seconds")


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    """One problem a reply has against its schema: the field it is at and what is wrong."""

    path: tuple[str | int, ...]
    """Where in the reply; empty when the reply as a whole is meant."""
    message: str
    """What is wrong, in the words of whoever validated the reply."""


class AgentOutputSchemaError(RuntimeError):
    """The provider gave up producing output that matches the requested response schema.

    Raised instead of a provider exit error so a caller can treat it like any
    other invalid structured response. ``detail`` is the provider's last
    validation errors and ``violations`` the same errors by field; a validator that
    reports only text yields one violation of the whole reply that holds the text.
    The conversation survives: the session keeps it, so a
    correction sent as the next turn continues the same work.
    """

    def __init__(
        self,
        detail: str,
        violations: tuple[SchemaViolation, ...] = (),
        *,
        usage: AgentUsage | None = None,
    ) -> None:
        """Record the validation errors and any usage measured before rejection."""
        self.detail = detail
        self.violations = violations or (SchemaViolation(path=(), message=detail),)
        self.usage = usage
        super().__init__(f"agent output did not match the response schema: {detail}")


class AgentContextOverflowError(RuntimeError):
    """The provider refused a turn because its conversation no longer fits the model's window.

    Raised instead of a provider exit error so the session owner can tell a conversation that
    outgrew its model from a failed turn: the work did not fail, and the same message may run
    in a fresh conversation. agentshim reports the condition as
    ``FailureKind.CONTEXT_OVERFLOW``, which ``shim_translation.failure_from`` maps to this.
    """

    def __init__(self, provider: str, detail: str) -> None:
        """Record the provider and its diagnostic."""
        self.provider = provider
        self.detail = detail
        super().__init__(f"{provider} context window exceeded: {detail}")


class QuotaCondition(StrEnum):
    """Which kind of capacity limit stopped a provider turn."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    """A usage quota, spend limit or billing limit: retrying now cannot succeed."""
    RATE_LIMITED = "rate_limited"
    """Sustained rate limiting: the provider's retries ran out while a window was exhausted."""
    UNAVAILABLE = "unavailable"
    """A provider outage: transient errors (an overload, a server error, a gateway with no
    accounts) outlasted the provider's retries, and no window was reported exhausted, so
    capacity may return at any moment."""


class AgentQuotaError(RuntimeError):
    """A turn failed because the provider has no capacity left, not because the work failed.

    Raised instead of a provider exit error so a caller can pause and wait for
    capacity instead of treating the turn as failed. The conversation is intact
    and the same turn may be sent again once capacity returns. ``resets_at`` is
    epoch seconds when the provider reported when the exhausted window reopens,
    else ``None``.
    """

    def __init__(
        self,
        provider: str,
        condition: QuotaCondition,
        detail: str,
        resets_at: float | None = None,
    ) -> None:
        """Record the provider, the condition, its diagnostic and the reset time."""
        self.provider = provider
        self.condition = condition
        self.detail = detail
        self.resets_at = resets_at
        super().__init__(f"{provider} {condition.value.replace('_', ' ')}: {detail}")


type QuotaResumeReason = Literal["operator", "wait_elapsed"]
"""Who ended a quota pause: the operator, or the policy's wait running out."""

type QuotaWaitCancelReason = Literal["resumed", "paused"]
"""How the operator ended a wait for capacity: resumed the run now, or paused it again."""

type ProviderSwitchReason = Literal["policy", "operator"]
"""Who switched the run to its fallback provider: the quota policy, or the operator."""


@dataclass(frozen=True, slots=True)
class Attribution:
    """Which agent, round and invocation an event belongs to; ``None`` when unknown."""

    agent_kind: str | None = None
    round_label: str | None = None
    invocation_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaPlan:
    """What happens next to a run paused on a capacity limit.

    ``policy`` names the run's unattended quota policy (``pause``, ``wait``, ``fail`` or
    ``fallback``).
    ``resumes_at`` is the epoch second the run resumes itself, when its policy
    does; ``fallback_provider`` and ``fallback_model`` name where an operator may
    resume it instead, when a fallback is configured.
    """

    policy: str = "pause"
    resumes_at: float | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSwitch:
    """A run's switch from one provider to its fallback, and why."""

    from_provider: str
    to_provider: str
    to_model: str
    reason: ProviderSwitchReason
    detail: str


class CapacityStops:
    """One turn's capacity stops so far, and whether its caller cancelled it.

    The client makes one record per turn, before its first send, and hands the same
    record to every gate call of that turn, so the record outlives each resend. It
    counts each stop in ``count`` (the first stop is 1) and calls :meth:`cancel` when
    the caller cancels the turn; it never sends a cancelled turn again. ``model`` is the
    model the turn runs on (``None`` for the provider's default), so a gate can tell one
    model that is down from another the same provider still serves.

    ``memory`` belongs to the gate: what it keeps about this turn between stops (its
    budgets, its outage backoff). The client never reads it. One record per turn keeps
    the budgets of turns that stop at the same time apart. A gate waits with
    :meth:`park`, which :meth:`cancel` and :meth:`interrupt` end early, and a gate that
    parks elsewhere learns of a cancellation through :meth:`on_cancel`.
    """

    def __init__(self, threads: Threads, *, model: str | None = None) -> None:
        """Start a turn with no stops; ``threads`` makes the event a wait parks on."""
        self.count = 0
        self.model = model
        self.memory: object | None = None
        self._cancelled = False
        self._wake = threads.event()
        self._lock = threads.lock()
        self._listeners: list[Callable[[], object]] = []

    @property
    def cancelled(self) -> bool:
        """Whether the caller cancelled this turn."""
        return self._cancelled

    def cancel(self) -> None:
        """Mark the turn cancelled, end a wait in progress and tell the cancel listeners."""
        with self._lock:
            self._cancelled = True
            listeners = tuple(self._listeners)
        self._wake.set()
        for listener in listeners:
            listener()

    def on_cancel(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call ``listener`` when the turn is cancelled; return its unsubscribe callable.

        A listener added after the cancellation is called at once. It runs on the
        cancelling thread and must not block.
        """
        with self._lock:
            already = self._cancelled
            if not already:
                self._listeners.append(listener)
        if already:
            listener()

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def interrupt(self) -> None:
        """End a wait in progress without cancelling the turn (a stop request, say)."""
        self._wake.set()

    def park(self, seconds: float) -> bool:
        """Wait at most ``seconds``, on the threads' clock; True when woken sooner.

        The wake is consumed, so the next wait starts clear; its causes (the
        cancellation, a run's stop request) stay readable where they are recorded.
        """
        woke = self._wake.wait(seconds)
        self._wake.clear()
        return woke


class CapacityGate(Protocol):
    """The caller's policy for a turn that stopped on a provider capacity limit.

    The client calls :meth:`wait_for_capacity` with the failure and returns
    only when the same turn should be sent again on the same conversation. It
    raises to end the turn instead, and the raised error replaces the quota
    error. The call may block for hours, so a gate owns its own cancellation:
    a stop request, and a cancellation of the turn (:meth:`CapacityStops.cancel`),
    must end the wait by raising.
    """

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        """Block until ``turn`` may be sent again, or raise to give up.

        ``stops`` is this turn's record, already counting this stop (see
        :class:`CapacityStops`).
        """
        ...

    def turn_served(self, provider: str, model: str | None) -> None:
        """Note that a turn on ``provider`` and ``model`` just succeeded.

        The client calls it after every successful turn, with the ``model`` it gave the
        turn's :class:`CapacityStops`, so the gate can tell a model that is down from
        one that fails only this turn.
        """
        ...


@runtime_checkable
class CapacityGated(Protocol):
    """An optional client capability: capacity limits go to an installed gate."""

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        """Route every later capacity limit through ``gate``."""
        ...


class AuthStatus(StrEnum):
    """Whether a provider CLI is logged in, as far as its own tooling can say."""

    OK = "ok"
    FAILED = "failed"
    #: The CLI offers no cheap check, or the check could not run. Not a problem.
    UNKNOWN = "unknown"


class ReadinessProblem(StrEnum):
    """Why a provider cannot start a turn."""

    BINARY_MISSING = "binary_missing"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """What a readiness probe found about one provider CLI, before any turn.

    ``detail`` explains ``auth`` in words fit for an error message, including
    the fix when it failed, or says the binary was not found.
    """

    provider: str
    binary_found: bool
    path: str | None
    version: str | None
    auth: AuthStatus
    detail: str

    @property
    def problem(self) -> ReadinessProblem | None:
        """Return what stops a turn from starting, or ``None`` when none is known."""
        if not self.binary_found:
            return ReadinessProblem.BINARY_MISSING
        if self.auth is AuthStatus.FAILED:
            return ReadinessProblem.AUTH_FAILED
        return None


_READINESS_FIXES = {
    ReadinessProblem.BINARY_MISSING: (
        "install the CLI where the agent runs (the sandbox image under --docker) or put it on PATH"
    ),
    ReadinessProblem.AUTH_FAILED: "log in to the CLI as the agent would run it",
}


class ProviderNotReadyError(RuntimeError):
    """A provider CLI cannot start a turn: it is missing or not logged in.

    Permanent: waiting or retrying cannot fix it, so it is not ``retryable``.
    Raised before the first turn, so no agent work was lost. ``problem`` and
    ``provider`` identify what to fix; the message names both, the diagnostic
    and the fix.
    """

    retryable = False

    def __init__(self, readiness: ProviderReadiness) -> None:
        """Build the message from a readiness with a known problem."""
        problem = readiness.problem
        if problem is None:
            message = "ProviderNotReadyError needs a readiness with a problem"
            raise ValueError(message)
        self.provider = readiness.provider
        self.problem = problem
        self.detail = readiness.detail
        super().__init__(
            f"{readiness.provider} agent is not ready ({problem.value}): {readiness.detail}. "
            f"Fix: {_READINESS_FIXES[problem]}."
        )


class AgentEventKind(StrEnum):
    """Semantic categories streamed by every provider."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    SKILL = "skill"
    """The agent loaded a skill; ``text`` is its name."""
    RATE_LIMIT = "rate_limit"
    """The provider reported one rate-limit window; see ``AgentEvent.rate_limit``."""


@dataclass(frozen=True, slots=True)
class AgentRateLimit:
    """One rate-limit window as the provider just reported it.

    A provider that reports several windows (a five-hour and a weekly one)
    emits one value per window; a consumer reads the newest per
    ``(limit, window)``. ``None`` means the provider did not state the value,
    never zero. ``resets_at`` is epoch seconds.
    """

    provider: str | None = None
    window: str | None = None
    limit: str | None = None
    used_fraction: float | None = None
    resets_at: float | None = None
    window_minutes: int | None = None
    exhausted: bool | None = None

    @property
    def is_exhausted(self) -> bool:
        """Whether the window has no capacity left.

        The provider's own statement wins. Without one, a window used up to
        or past 100% counts as exhausted; an unstated usage does not.
        """
        if self.exhausted is not None:
            return self.exhausted
        return self.used_fraction is not None and self.used_fraction >= 1.0


@dataclass(frozen=True, slots=True)
class AgentExecutionPolicy:
    """Existing sandbox semantics that a session launcher must enforce.

    A launcher must reject policies it cannot fully enforce. The reusable
    ``vs_sandbox`` library remains the authority for project-path and host-
    resource semantics; this value only attaches them to agent setup.
    """

    project_paths: ProjectPathPolicy | None = None
    host_resources: tuple[HostResource, ...] = ()
    require_enforcement: bool = True
    containerized: bool = False


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """Provider-independent description of a session-scoped stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    runtime_env: tuple[tuple[str, str], ...] = field(default=(), compare=False, repr=False)
    runtime_env_keys: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        """Keep transport key names in identity and reject authority overrides."""
        keys = tuple(sorted(key for key, _ in self.runtime_env))
        overlap = set(keys).intersection(key for key, _ in self.env)
        if overlap:
            message = f"runtime_env overlaps identity environment keys: {sorted(overlap)}"
            raise ValueError(message)
        object.__setattr__(self, "runtime_env_keys", keys)


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Features an agent client can provide without weakening requested semantics."""

    tool_servers: bool = False
    nested_read_only_paths: bool = False
    hidden_paths: bool = False
    host_path_grants: bool = False
    container_execution: bool = False
    timeouts: bool = False
    session_reuse: bool = True
    # Whether a session can adopt a provider conversation ID produced by an
    # earlier process, so a resumed run continues the same conversation instead
    # of replaying it. ``session_reuse`` only promises reuse within one process.
    provider_session_resume: bool = False
    # Whether a session is offered only the run's skills, never the operator's
    # personal or plugin skills from the host's provider state.
    skill_isolation: bool = False
    # Whether a session is connected only to the MCP servers the run
    # configured, never the operator's own (user or project configuration,
    # plugins, account connectors).
    mcp_isolation: bool = False
    # Whether a session loads none of the operator's own CLI configuration:
    # user settings, hooks, global instructions, notify commands, memory.
    config_isolation: bool = False


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """Provider-independent token and cost accounting for one turn.

    ``None`` means unknown, including an increment whose resumed conversation
    has no accounting baseline. Summing known counts yields only a lower bound
    when any contributing turn is unknown.
    """

    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    reasoning_output_tokens: int | None = None
    """The part of ``output_tokens`` the provider reports as reasoning (never in addition to it)."""


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One normalized event emitted while an agent turn is running.

    A ``THINKING`` event carrying ``payload={"channel": "diagnostic"}`` is
    provider plumbing (a provider heartbeat, a stderr line, a thread or turn
    marker), not the agent's reasoning. Consumers route it to the diagnostic
    channel so a transcript never presents plumbing as chain of thought. A
    provider that has no separate diagnostic stream marks such events itself;
    every other ``THINKING`` event is agent reasoning.
    """

    kind: AgentEventKind
    text: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    usage: AgentUsage | None = None
    rate_limit: AgentRateLimit | None = None


class AgentObserver(Protocol):
    """Consumer of normalized streaming events from an agent provider."""

    def on_event(self, event: AgentEvent) -> None:
        """Observe one event in emission order."""
        ...


@dataclass(frozen=True, slots=True)
class AgentSessionSpec:
    """Immutable configuration installed once for a conversation."""

    role: str
    provider: str
    workspace: Path
    policy: AgentExecutionPolicy
    model: str | None = None
    scripted: bool = False
    """True when a scripted agent runs the turn. The recorded fact is then scripted, not this model."""
    mcp_servers: tuple[MCPServerSpec, ...] = ()
    skills: tuple[Path, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    reasoning_effort: str | None = None


def session_spec_fingerprint(spec: AgentSessionSpec) -> str:
    """Return a stable digest of session identity and capabilities.

    ``AgentClient`` drops a cached session as soon as its spec stops matching
    the requested one. A resumed process has no earlier spec object to compare
    against, so it compares this digest instead: same inputs, same rule, so any
    identity or capability change that would evict a live session also refuses a
    checkpointed provider conversation. MCP launch credentials and endpoints
    are excluded through their explicit ``runtime_env`` field. They are freshly
    supplied to the launcher without entering durable state. The digest is content-derived rather
    than a Python ``hash``, which is not stable across processes.
    """
    return hashlib.sha256(repr(spec).encode("utf-8")).hexdigest()


DEFAULT_TURN_TIMEOUT_S = 6 * 60 * 60
"""The bound on a turn when neither the turn nor its session names one.

A hang guard, not a budget: no provider turn should run six hours, and a run
with no ``cli_timeout`` and no ``max_run_seconds`` would otherwise wait for ever
on a provider process that stopped answering.
"""


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    """Context added to an existing conversation for one agent turn."""

    message: str
    instructions: str = ""
    output_schema: type[BaseModel] | None = None
    timeout: timedelta | None = None
    """This turn's bound; ``None`` takes the session's, then ``DEFAULT_TURN_TIMEOUT_S``."""
    invocation_id: str | None = None
    label: str | None = None
    expected_provider_session_id: str | None = None
    """Strict continuation: refuse any reset, replacement, or fresh session."""
    require_provider_checkpoint: bool = False
    """Retain resumable history after this turn, including an initial dispatch."""
    renewable: bool = False
    """The message stands alone: it may run in a fresh conversation instead of the expected one.

    Set only for a turn whose message carries the role's whole prompt, never for one that
    answers the conversation (a correction, a resumed wait). The session owner then retires a
    conversation near its model's context window before the turn, and runs the turn once more
    in a fresh conversation after the provider refused it for context overflow
    (``AgentContextOverflowError``). A strict continuation (``expected_provider_session_id``)
    is checked against the conversation before it is retired.
    """


@dataclass(frozen=True, slots=True)
class AgentSkillUse:
    """Skills a turn was offered and loaded, as the provider reported them.

    ``None`` means the provider cannot say, never zero: a provider without a
    skill signal leaves both fields unset. ``invoked`` has one entry per load,
    in order, so its length is the turn's skill-use count.
    """

    offered: tuple[str, ...] | None = None
    invoked: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Provider-independent result of one raw agent turn."""

    text: str
    usage: AgentUsage = field(default_factory=AgentUsage)
    provider_session_id: str | None = None
    restarted: bool = False
    """The conversation this turn ran in was retired or replaced.

    The session no longer holds the history the turn built on, so a caller that
    keeps a checkpoint of it must drop the checkpoint. The turn's own answer
    is still valid.
    """
    skills: AgentSkillUse = field(default_factory=AgentSkillUse)
    refused_steers: tuple[str, ...] = ()
    """The ids of the keyed steers this turn's provider took and then did not keep.

    Each steer a keyed call answered ``DELIVERED`` (``steer_session``) ends the turn either
    consumed or here: refused by the provider, or still unresolved when the provider stopped
    (a quota stop before the turn was sent again counts). The model did not read these.
    """


class SteerOutcome(StrEnum):
    """What became of a message offered to a running turn."""

    DELIVERED = "delivered"
    """The provider accepted it into the running turn."""
    UNSUPPORTED = "unsupported"
    """This provider or transport cannot take a message mid-turn."""
    NO_RUNNING_TURN = "no_running_turn"
    """No turn could take it right now: none is running, or it is starting or finishing."""


@runtime_checkable
class SteerableAgentClient(Protocol):
    """An optional client capability: offer a message to the turn running now."""

    def steer(self, text: str, *, on_rejected: Callable[[], None]) -> SteerOutcome:
        """Offer *text* to the in-flight turn; see :meth:`vs_agent.shim_turns.LaunchedSession.steer`."""
        ...


@runtime_checkable
class ReadinessProbe(Protocol):
    """An optional launcher capability: check the provider CLI before any session exists.

    A launcher whose provider tooling has no such check does not implement it,
    and callers skip the check rather than guess.
    """

    def probe_readiness(self, spec: AgentSessionSpec) -> ProviderReadiness:
        """Probe the CLI *spec* would launch, through the same route as its session.

        A missing binary is a result, not an exception. No model is called.
        """
        ...


class AgentClientProtocol(Protocol):
    """The agent-service surface the run context and every loop depend on.

    Each backend supplies one implementation: the CLI
    :class:`~vs_agent.client.AgentClient`, the deterministic stub, and
    the plain loop's tracker wrapper. Attribution
    (``backend_name``, ``provider``, ``model_for_kind``) is
    part of this contract because the loop stamps it onto every round record,
    so a consumer never has to probe an implementation for it.
    """

    @property
    def backend_name(self) -> str:
        """Return the configured agent backend (``"cli"``, ``"stub"``, ...)."""
        ...

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this client's execution system can enforce."""
        ...

    @property
    def provider(self) -> str | None:
        """Return the provider that runs this client's turns."""
        ...

    def model_for_kind(self, kind: str) -> str | None:
        """Return the effective model for ``kind``, honoring role overrides."""
        ...

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the next turn on ``session_key`` continues.

        ``None`` means the next turn starts from no history at all.
        """
        ...

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation ``session_key``'s last turn ran in."""
        ...

    def invoke(  # noqa: PLR0913  # lint-waiver: LW-010126 [PLR0913]; Preserve AgentClientProtocol.invoke's named-argument contract because callers pass these independent settings directly.
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        tool_servers: list[ToolServerDescriptor] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> T:
        """Run one turn and parse its structured response."""
        ...

    def invoke_text(  # noqa: PLR0913  # lint-waiver: LW-010127 [PLR0913]; Preserve AgentClientProtocol.invoke_text's named-argument contract because callers pass these independent settings directly.
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
        tool_servers: list[ToolServerDescriptor] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Run one conversational turn without a structured-output requirement."""
        ...

    def set_log_file(self, stream: TextIO | None) -> None:
        """Direct subsequent application logs to ``stream``."""
        ...

    def cancel(self) -> None:
        """Stop every in-flight turn without closing the client."""
        ...

    def close(self) -> None:
        """Release client resources. Implementations must be idempotent."""
        ...
