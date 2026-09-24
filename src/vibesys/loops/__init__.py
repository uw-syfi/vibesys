"""The three outer loops + their shared infra.

Each subpackage corresponds to one ``--outer-loop`` value:

  - ``loops.agent``   — orchestrator-driven, roadmap.md as issue board
  - ``loops.plain``   — deterministic queue drain, IssueBoard (issues.json)
  - ``loops.evolve``  — population-based mutation/selection

``loops.profiler`` is the shared profiler invocation helper used by
``agent`` and ``evolve`` (``plain`` does not run a profiler step today).

This package's ``__init__.py`` is intentionally empty so importing one
policy does not import the others or their optional dependencies.
"""
