"""Build the :class:`~vibe_serve._agent_cli.MCPServerSpec` for the issue tracker.

The issue-loop hands one of these to ``CliAgentRunner.invoke(mcp_servers=...)``
each phase. The runner then forwards it to the active provider's
``install_mcp_servers`` hook, which serializes the spec into whatever config
format that CLI expects (``.mcp.json`` for Claude, ``.gemini/settings.json``
for Gemini, ``opencode.json`` for opencode, ``--config`` flags for Codex).

This module is the only piece of issue-tracker-specific knowledge in the
MCP path; everything else is provider-agnostic and lives in
``vibe_serve/_agent_cli/``.
"""

from __future__ import annotations

from vibe_serve._agent_cli.base import MCPServerSpec
from vs_issue_board import IssueType


def build_issue_mcp_spec(
    *,
    store_relpath: str,
    creator: str,
    iteration: int,
    cap: int | None,
    allowed_types: set[IssueType],
) -> MCPServerSpec:
    """Build the per-phase :class:`MCPServerSpec` for the issue tracker.

    The args list is consumed by ``vs_issue_board.mcp``'s argparse-based CLI
    (positional ``store_path`` plus the policy flags).
    """
    args = [
        "-m",
        "vs_issue_board.mcp",
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
