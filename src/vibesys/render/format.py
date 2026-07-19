"""Plain-text formatting shared by the terminal renderer and the run log."""

from __future__ import annotations

from vibesys.server.events import AgentStatusData


def format_token_count(n: int) -> str:
    """Format a token count compactly: ``999`` / ``20k`` / ``1.0M``."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


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
