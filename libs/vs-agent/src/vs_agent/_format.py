"""Plain-text status-prefix formatting for agent callback log lines.

The agent package owns its log-line rendering independently of product
frontends such as ``headless``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_agent.contracts import AgentRateLimit
    from vs_agent.events import AgentStatusData

_THOUSAND = 1_000
_MILLION = 1_000_000


def format_token_count(n: int) -> str:
    """Format a token count compactly: ``999`` / ``20k`` / ``1.0M``."""
    if n < _THOUSAND:
        return str(n)
    if n < _MILLION:
        return f"{n // 1000}k"
    return f"{n / _MILLION:.1f}M"


def format_status_prefix(status: AgentStatusData | None) -> str:
    """Build the ``[progress | label | elapsed | tokens/max] `` status prefix.

    Returns an empty string when there is nothing identifying to show
    (no progress reading and no agent label), matching the historical
    behavior of anonymous ``AgentLogger`` instances.
    """
    if status is None:
        return ""
    if not status.progress and not status.agent_label:
        return ""
    used = format_token_count(status.input_tokens)
    if status.context_window:
        tokens_str = f"{used}/{format_token_count(status.context_window)}"
    else:
        tokens_str = used
    parts: list[str] = []
    if status.progress:
        parts.append(status.progress)
    if status.agent_label:
        parts.append(status.agent_label)
    parts.extend([f"{status.elapsed_seconds:.1f}s", tokens_str])
    return f"[{' | '.join(parts)}] "


def format_rate_limit(rate_limit: AgentRateLimit) -> str:
    """Build the run-log line for one provider-reported rate-limit window."""
    name = " ".join(
        part for part in (rate_limit.provider, rate_limit.limit, rate_limit.window) if part
    )
    state = "EXHAUSTED" if rate_limit.is_exhausted else "ok"
    parts = [f"[rate limit] {name or 'window'}: {state}"]
    if rate_limit.used_fraction is not None:
        parts.append(f"{rate_limit.used_fraction:.0%} used")
    if rate_limit.resets_at is not None:
        reset = datetime.fromtimestamp(rate_limit.resets_at, tz=UTC)
        parts.append(f"resets {reset:%Y-%m-%d %H:%M} UTC")
    return ", ".join(parts)
