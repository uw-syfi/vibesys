"""Pure, deterministic search policy packages.

Every module under ``vibesys.search`` answers questions about round history
and returns new state; it never decides which agent runs next, never touches
``RunContext``, prompts, or the filesystem, and never reads a clock or the
global RNG. State is a plain serializable pydantic value that orchestration
persists.
"""

from __future__ import annotations
