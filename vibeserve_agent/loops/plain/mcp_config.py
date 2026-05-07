"""Build the :class:`~libs.agent_cli.MCPServerSpec` for the issue tracker.

The issue-loop hands one of these to ``CliAgentRunner.invoke(mcp_servers=...)``
each phase. The runner then forwards it to the active provider's
``install_mcp_servers`` hook, which serializes the spec into whatever config
format that CLI expects (``.mcp.json`` for Claude, ``.gemini/settings.json``
for Gemini, ``opencode.json`` for opencode, ``--config`` flags for Codex).

This module is the only piece of issue-tracker-specific knowledge in the
MCP path; everything else is provider-agnostic and lives in
``libs/agent_cli/``.
"""

from __future__ import annotations

from libs.agent_cli.base import MCPServerSpec

from vibeserve_agent.loops.plain.issue_board import IssueType


def build_issue_mcp_spec(
    *,
    store_relpath: str,
    creator: str,
    iteration: int,
    cap: int | None,
    allowed_types: set[IssueType],
) -> MCPServerSpec:
    """Build the per-phase :class:`MCPServerSpec` for the issue tracker.

    The args list is consumed by ``vibeserve_agent.loops.plain.mcp_server``'s
    argparse-based CLI (positional ``store_path`` plus the policy flags).
    Both host and Docker modes use ``python -m vibeserve_agent.loops.plain.mcp_server``
    so the same module-form invocation works inside the container with
    ``PYTHONPATH=/opt/vibeserve``, without needing the package pip-installed.
    """
    args = [
        "-m",
        "vibeserve_agent.loops.plain.mcp_server",
        store_relpath,
        "--creator",
        creator,
        "--iteration",
        str(iteration),
        "--allowed-types",
        ",".join(sorted(t.value for t in allowed_types)),
    ]
    if cap is not None:
        args += ["--cap", str(cap)]
    return MCPServerSpec(
        name="vibeserve-issues",
        command="python",
        args=args,
    )
