"""Shared profiler invocation helpers.

Two loops drive the Profiler agent today: ``agent/loop.py`` (per-round
profiling, owns the round/progress.md side-effects) and
``evolve/loop.py`` (per-offspring profiling, with an optional
Pareto-frontier addendum).  Both build an MCP server spec for the
analysis tools (torch profiler or nsys), render their own system
prompt, and call ``ctx.invoke(kind="profiler", ...)`` with a
``ProfilerSummary`` fallback.

This module owns the parts that are identical across the two: the
``MCPServerSpec`` factory and the agent-invocation wrapper. Each loop
still renders its own prompt (the templates and bound variables differ)
and decides what to do with the returned summary.
"""

from __future__ import annotations

from vibeserve_agent.schemas import ProfilerSummary


def mcp_spec(profiler_kind: str):
    """Build an ``MCPServerSpec`` that spawns the analysis MCP server.

    Returns ``None`` when ``libs.agent_cli`` is not importable in the
    current environment (e.g. a unit-test process that doesn't pull in
    the cli runner).  Callers treat ``None`` as "skip MCP"; the
    profiler agent still runs, just without tool access.
    """
    try:
        from libs.agent_cli import MCPServerSpec
    except Exception:
        return None
    if profiler_kind == "torch":
        return MCPServerSpec(
            name="vibeserve-torch-profiler",
            command="python",
            args=["torch_profiler/server.py"],
        )
    return MCPServerSpec(
        name="vibeserve-nsys-profiler",
        command="python",
        args=["nsys_profiler/server.py"],
    )


def invoke_profiler(
    ctx,
    *,
    system_prompt: str,
    round_label: str,
    fallback_suggestions: str = "Re-run profiling on the next round.",
) -> ProfilerSummary | None:
    """Run the Profiler agent and return its :class:`ProfilerSummary`.

    Side-effect free: the caller owns logging the result, writing it to
    progress.md, snapshotting the workspace, etc. Returns ``None`` on
    exception (the caller decides whether that's fatal).
    """
    spec = mcp_spec(ctx.profiler_kind)
    try:
        return ctx.invoke(
            kind="profiler",
            system_prompt=system_prompt,
            user_prompt=(
                "Profile the server and return exactly one JSON object "
                "matching the schema above."
            ),
            response_cls=ProfilerSummary,
            fallback_factory=lambda: ProfilerSummary(
                analysis="Profiler produced no structured response.",
                bottlenecks="n/a",
                suggestions=fallback_suggestions,
                perf_metric=None,
                perf_unit=None,
            ),
            round_label=round_label,
            mcp_servers=[spec] if spec is not None else None,
        )
    except Exception as exc:
        ctx.lprint(f"[warn] profiler failed: {exc}")
        return None
