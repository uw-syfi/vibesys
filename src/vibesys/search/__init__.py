"""Deterministic, pure search guidance.

Modules under ``vibesys.search`` answer selection/progress questions and
return new state; they never hold a :class:`~vibesys.orchestration.runtime.RunContext`,
spawn agents, render prompts, touch the filesystem, or read a clock or the
global :mod:`random` module. Orchestration (``vibesys.loops``) is the only
caller and owns persisting the returned state.
"""

from __future__ import annotations
