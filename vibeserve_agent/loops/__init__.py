"""The three outer loops + their shared infra.

Each subpackage corresponds to one ``--outer-loop`` value:

  - ``loops.agent``   — orchestrator-driven, roadmap.md as issue board
  - ``loops.plain``   — deterministic queue drain, IssueBoard (issues.json)
  - ``loops.evolve``  — population-based mutation/selection

``loops.profiler`` is the shared profiler invocation helper used by
``agent`` and ``evolve`` (``plain`` does not run a profiler step today).

This package's ``__init__.py`` is intentionally empty so submodules with
lightweight footprints (e.g. ``loops.plain.mcp_server``, spawned inside
Docker containers that only have ``mcp>=1.0`` installed) don't drag in
heavy optional dependencies via package-level re-exports.
"""
