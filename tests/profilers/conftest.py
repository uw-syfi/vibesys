"""Hypothesis defaults for the profiler tool-server tests.

These tests exercise pure parsers, analyzers and in-process state machines, so a
handful of examples per property is enough at pull-request depth; nightly goes deep.
Pinned ``@example`` counterexamples carry the regression power. A test that wants a
different count passes its own ``settings(max_examples=examples(...))``.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

from vs_sim.api.testing import examples

settings.register_profile(
    "profilers",
    max_examples=examples(pr=6, nightly=100),
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("profilers")
