"""VibeSys outer-loop drivers and supporting runtime.

This package's ``__init__.py`` is intentionally empty so that submodules
with lightweight import footprints (notably ``vs_issue_board.mcp``,
which the plain loop's .mcp.json sandwich spawns inside Docker containers
that only have ``mcp>=1.0,<2`` installed) don't drag in heavy optional
dependencies like ``langchain_core`` via package-level re-exports.

Import what you need by full module path, e.g.::

    from vibesys.agents import build_agent_client
    from vibesys.loops.agent.loop import run_agent_loop
"""
