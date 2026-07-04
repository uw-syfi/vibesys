"""vibe_serve — agent, plain, and evolve outer-loop drivers.

This package's ``__init__.py`` is intentionally empty so that submodules
with lightweight import footprints (notably ``vs_issue_board.mcp``,
which the plain loop's .mcp.json sandwich spawns inside Docker containers
that only have ``mcp>=1.0`` installed) don't drag in heavy optional
dependencies like ``langchain_core`` via package-level re-exports.

Import what you need by full module path, e.g.::

    from vibe_serve.agents.callbacks import AgentLogger
    from vibe_serve.loops.agent.loop import run_agent_loop
"""
