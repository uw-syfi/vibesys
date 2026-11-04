"""The one place VibeSys types and agentshim types are converted into each other.

agentshim owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags). VibeSys owns the persisted and streamed shapes
(:class:`~vs_agent.contracts.AgentEvent`, :class:`~vs_agent.contracts.AgentUsage`,
:class:`~vs_agent.contracts.ProviderReadiness`, the error types callers catch).
Every conversion between the two lives here as a pure function, so a library
upgrade that renames a field changes this file and nothing else, and the rest
of ``vs_agent`` reads agentshim only for what it must call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import agentshim

from vs_agent.container_python import containerize_server
from vs_agent.contracts import (
    AgentContextOverflowError,
    AgentEvent,
    AgentEventKind,
    AgentModelUsage,
    AgentOutputSchemaError,
    AgentQuotaError,
    AgentRateLimit,
    AgentSkillUse,
    AgentSpawnError,
    AgentTurnTimeoutError,
    AgentUsage,
    AuthStatus,
    CostBasis,
    MCPServerSpec,
    ProviderReadiness,
    QuotaCondition,
    UsageSpeed,
)
from vs_agent.events import CommandResultPayload

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_PYTHON_MCP_COMMANDS = frozenset({"python", "python3"})
_DIAGNOSTIC_PAYLOAD: Mapping[str, object] = {"channel": "diagnostic"}

_AUTH_STATUS = {
    agentshim.AuthState.KNOWN_OK: AuthStatus.OK,
    agentshim.AuthState.FAILED: AuthStatus.FAILED,
    agentshim.AuthState.UNKNOWN: AuthStatus.UNKNOWN,
}

# Total on purpose: a member agentshim adds that these tables miss is a KeyError in the first test
# that walks the enum, never a silently dropped fact.
_COST_BASIS = {
    agentshim.CostSource.PRICE_TABLE: CostBasis.PRICE_TABLE,
    agentshim.CostSource.PROVIDER_REPORTED: CostBasis.PROVIDER_REPORTED,
    agentshim.CostSource.UNKNOWN_MODEL: CostBasis.UNKNOWN_MODEL,
    agentshim.CostSource.NOT_REPORTED: CostBasis.NOT_REPORTED,
}
_SPEED = {
    agentshim.UsageSpeed.STANDARD: UsageSpeed.STANDARD,
    agentshim.UsageSpeed.FAST: UsageSpeed.FAST,
    agentshim.UsageSpeed.ULTRAFAST: UsageSpeed.ULTRAFAST,
}


# --- requests: VibeSys to agentshim ------------------------------------------


def mcp_server_from(
    spec: MCPServerSpec,
    agent_path: Callable[[str], str],
    *,
    containerized: bool,
) -> agentshim.StdioMcpServer:
    """Translate one VibeSys MCP spec into the library's stdio spec.

    A bare ``python`` is whatever the agent's PATH resolves, which may lack the
    MCP dependencies or the framework. On the host the interpreter running
    VibeSys itself replaces it. In a container (*containerized*) the agent
    image's own interpreter and the framework's import roots do
    (:func:`~vs_agent.container_python.containerize_server`). Every absolute
    path in the command or args -- including that interpreter -- is then mapped
    through *agent_path* so a file the sandbox presents at a different location
    (a bind-mounted workspace, say) still resolves; a relative argument such as
    a flag or a workspace-relative path is left untouched.
    """
    if containerized:
        spec = containerize_server(spec)
    command = (
        sys.executable
        if not containerized and spec.command in _PYTHON_MCP_COMMANDS
        else spec.command
    )
    return agentshim.StdioMcpServer(
        name=spec.name,
        command=_mapped_if_absolute(command, agent_path),
        args=tuple(_mapped_if_absolute(arg, agent_path) for arg in spec.args),
        env={**dict(spec.env), **dict(spec.runtime_env)},
    )


def _mapped_if_absolute(value: str, agent_path: Callable[[str], str]) -> str:
    return agent_path(value) if value.startswith("/") else value


# --- results: agentshim to VibeSys -------------------------------------------


def usage_from(
    usage: agentshim.ProviderUsage,
    *,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
) -> AgentUsage:
    """Map library token accounting onto the neutral usage contract.

    ``input_tokens`` includes cached tokens on every provider: the library
    folds Anthropic's disjoint cache counts into the input total so the same
    field means the same thing everywhere. The token fields count the main
    conversation only, as the library's ``tokens`` do; ``by_model`` carries
    every model the turn ran, priced by the library and copied here unchanged,
    so the sum of its rows is the whole turn. ``cost_basis`` names how the
    library reached the cost.
    """
    if not usage.increment_known:
        # Unknown resumed totals are zero placeholders, not measured increments.
        # Duration belongs to this invocation and is independent of its tokens.
        return AgentUsage(duration_ms=duration_ms)
    tokens = usage.tokens
    return AgentUsage(
        input_tokens=tokens.input_tokens,
        cache_creation_input_tokens=tokens.cache_write_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
        output_tokens=tokens.output_tokens,
        reasoning_output_tokens=tokens.reasoning_output_tokens,
        total_cost_usd=cost_usd if cost_usd is not None else usage.total_cost_usd,
        duration_ms=duration_ms,
        cost_basis=None if usage.cost_source is None else _COST_BASIS[usage.cost_source],
        by_model=tuple(model_usage_from(row) for row in usage.by_model),
    )


def model_usage_from(row: agentshim.ModelUsage) -> AgentModelUsage:
    """Carry one model's share of a turn over, with the cost the library priced it at."""
    tokens = row.tokens
    return AgentModelUsage(
        model=row.model,
        input_tokens=tokens.input_tokens,
        cache_creation_input_tokens=tokens.cache_write_input_tokens,
        cache_creation_1h_input_tokens=tokens.cache_write_1h_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
        output_tokens=tokens.output_tokens,
        reasoning_output_tokens=tokens.reasoning_output_tokens,
        turns=tokens.turns,
        recorded_usd=row.recorded_usd,
        speed=None if row.speed is None else _SPEED[row.speed],
        primary=row.primary,
    )


def skill_use_from(summary: agentshim.SkillSummary) -> AgentSkillUse:
    """Carry the library's skill summary over, keeping unknown distinct from zero."""
    invocations = summary.invocations
    return AgentSkillUse(
        offered=summary.discovered,
        invoked=None if invocations is None else tuple(event.name for event in invocations),
    )


def result_text(result: agentshim.TurnResult) -> str:
    """Return the turn's answer, preferring the schema-conformant payload.

    Callers parse a structured turn's text back into their response model, so
    a provider that reported the payload out of band still has to deliver it
    as text.
    """
    if result.structured_output is not None:
        return json.dumps(result.structured_output)
    return result.text


def readiness_from(status: agentshim.ProviderStatus) -> ProviderReadiness:
    """Translate the library's probe result into the neutral readiness contract."""
    return ProviderReadiness(
        provider=status.provider,
        binary_found=status.binary_found,
        path=status.path,
        version=status.version,
        auth=_AUTH_STATUS[status.auth],
        detail=status.auth_detail,
    )


# --- failures: agentshim to the errors callers catch -------------------------


def spawn_error_from(provider: str, error: BaseException) -> AgentSpawnError:
    """Describe a process that could not be started."""
    return AgentSpawnError(provider, str(error))


def timeout_error_from(error: agentshim.TurnTimeoutError) -> AgentTurnTimeoutError:
    """Carry the exceeded budget and any partial accounting over.

    A one-shot process reports the budget as ``CliTimeoutError``; a long-lived
    one raises its parent, ``TurnTimeoutError``. Only the one-shot error can
    carry the parsed prefix that was received before the process timed out.
    """
    partial = error.partial if isinstance(error, agentshim.CliTimeoutError) else None
    usage = usage_from(partial.usage, cost_usd=partial.cost_usd) if partial is not None else None
    session_id = None if partial is None else partial.session_id
    return AgentTurnTimeoutError(error.timeout, usage=usage, provider_session_id=session_id)


def failure_from(
    error: agentshim.TurnFailedError,
    *,
    provider: str,
    exhausted: Sequence[agentshim.RateLimitStatus],
) -> AgentOutputSchemaError | AgentQuotaError | AgentContextOverflowError | None:
    """Classify a failed turn as a schema rejection, a context overflow or a capacity limit.

    Returns ``None`` for any other failure, which the caller re-raises as it
    came.

    A provider that gave up matching the output schema
    (``FailureKind.SCHEMA``) is an :class:`AgentOutputSchemaError` carrying its
    validation errors. A conversation that no longer fits the model's context window
    (``FailureKind.CONTEXT_OVERFLOW``) is an :class:`AgentContextOverflowError`: the work did
    not fail, and the same turn may run in a fresh conversation. agentshim classifies a usage
    or spend limit as
    ``USAGE_LIMIT`` and has already waited out the transient failures it can. A
    ``TRANSIENT`` failure that outlasted those waits is sustained rate limiting
    when the provider also reported an exhausted window during the turn, and a
    provider outage (an overload, a server error) when it did not. Either way
    the work did not fail and the same turn may be sent again, so both are
    capacity limits the caller's gate waits out instead of failing the run. The
    reset time is the latest of the *exhausted* windows, since capacity returns
    only when every one of them has reopened.
    """
    if error.kind is agentshim.FailureKind.SCHEMA:
        return AgentOutputSchemaError(error.detail)
    if error.kind is agentshim.FailureKind.CONTEXT_OVERFLOW:
        return AgentContextOverflowError(provider, error.detail or str(error))
    if error.kind is agentshim.FailureKind.USAGE_LIMIT:
        condition = QuotaCondition.QUOTA_EXHAUSTED
    elif error.kind is agentshim.FailureKind.TRANSIENT:
        condition = QuotaCondition.RATE_LIMITED if exhausted else QuotaCondition.UNAVAILABLE
    else:
        return None
    resets = [window.resets_at for window in exhausted if window.resets_at is not None]
    return AgentQuotaError(
        provider,
        condition,
        error.detail or str(error),
        max(resets) if resets else None,
    )


# --- events: agentshim to VibeSys --------------------------------------------


def _diagnostic(text: str) -> AgentEvent:
    """Report provider plumbing on the diagnostic channel, not as reasoning."""
    return AgentEvent(kind=AgentEventKind.THINKING, text=text, payload=_DIAGNOSTIC_PAYLOAD)


def event_from(  # one arm per event type
    event: agentshim.AgentEvent,
    *,
    structured: bool,
) -> AgentEvent | None:
    """Translate one library event, or return ``None`` to drop it.

    *structured* says the turn asked for a response schema. The assistant text
    of such a turn is the raw schema payload, which reaches the caller through
    :attr:`AgentTurnResult.text` and is rendered from the parsed model. Putting
    it on the assistant channel as well would stream unformatted JSON and then
    repeat it, so a structured turn's text goes to the diagnostic channel.
    """
    if isinstance(event, agentshim.AssistantText):
        return (
            _diagnostic(event.text)
            if structured
            else AgentEvent(kind=AgentEventKind.TEXT, text=event.text)
        )
    if isinstance(event, agentshim.Reasoning):
        return AgentEvent(kind=AgentEventKind.THINKING, text=event.text)
    if isinstance(event, agentshim.ToolCall):
        return AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            payload={"tool": event.tool, "args": event.args if event.args is not None else {}},
        )
    if isinstance(event, agentshim.ToolResult):
        return AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            text=event.stdout or event.stderr,
            payload={
                "tool": event.tool,
                "stdout": event.stdout,
                "stderr": event.stderr,
                "exit_code": event.exit_code,
                "duration": event.duration_s,
                "result_payload": CommandResultPayload(
                    stdout=event.stdout,
                    stderr=event.stderr,
                    exit_code=event.exit_code,
                    duration=event.duration_s,
                ),
            },
        )
    if isinstance(event, agentshim.UsageReport):
        return AgentEvent(
            kind=AgentEventKind.USAGE,
            usage=usage_from(event.usage, cost_usd=event.cost_usd),
        )
    return (
        _skill_event_from(event)
        or _rate_limit_event_from(event)
        or _steer_event_from(event)
        or _plumbing_event_from(event)
    )


def _skill_event_from(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate a skill load, and log the offered list where a run log shows it.

    Which provider frames mean a skill was offered or loaded is agentshim's
    knowledge; this only maps its typed events.
    """
    if isinstance(event, agentshim.SkillInvoked):
        return AgentEvent(
            kind=AgentEventKind.SKILL,
            text=event.name,
            payload={"skill": event.name, "source_path": event.source_path},
        )
    if isinstance(event, agentshim.SkillsDiscovered):
        return _diagnostic(f"[skills offered] {', '.join(event.names) or '(none)'}")
    return None


def _rate_limit_event_from(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate the provider's report of one rate-limit window."""
    if not isinstance(event, agentshim.RateLimitStatus):
        return None
    return AgentEvent(
        kind=AgentEventKind.RATE_LIMIT,
        rate_limit=AgentRateLimit(
            window=event.window,
            limit=event.limit,
            used_fraction=event.used_fraction,
            resets_at=event.resets_at,
            window_minutes=event.window_minutes,
            exhausted=event.exhausted,
        ),
    )


def _steer_event_from(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Report what became of an operator message sent into the running turn."""
    if isinstance(event, agentshim.SteerDelivered):
        return _diagnostic("[steer] the provider accepted an operator message mid-turn")
    if isinstance(event, agentshim.SteerConsumed):
        return _diagnostic("[steer] the model took the operator message into the running turn")
    if isinstance(event, agentshim.SteerRejected):
        return _diagnostic(
            f"[steer] the provider refused the operator message ({event.reason}); "
            "it is delivered at the next turn boundary instead"
        )
    return None


def _plumbing_event_from(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate the events that describe the provider, not the agent."""
    if isinstance(event, agentshim.SessionStarted):
        return _diagnostic(f"[session {event.session_id} started]")
    if isinstance(event, agentshim.Lifecycle):
        return _diagnostic(f"[{event.kind}] {event.detail}" if event.detail else f"[{event.kind}]")
    if isinstance(event, agentshim.Stderr):
        return _diagnostic(f"[stderr] {event.text}")
    if isinstance(event, agentshim.RawOutput):
        return _diagnostic(event.text)
    if isinstance(event, agentshim.ProviderError):
        return _diagnostic(f"[error] {event.message}")

    # RunStarted and RunFinished describe the subprocess, not the agent.
    return None


def default_agent_path(path: Path | str, /) -> str:
    """Return *path* unchanged: the agent sees host paths as they are."""
    return str(Path(path))
