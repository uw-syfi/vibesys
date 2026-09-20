"""Typed identity of one reusable agent conversation.

:class:`AgentClient` caches a live session per key and, for the scopes that opt
in, checkpoints that session's provider conversation ID under the same key. The
key therefore decides two things at once: which turns share a conversation, and
which conversations survive a restart.

The serialized form is ``"<scope>:<identifier>"``, e.g. ``"hypothesis:H-01"``,
which is what the machine-local session map is keyed by on disk.

The canonical definitions now live in ``vs_agent.session_key``; this module
re-exports them so existing importers keep working.
"""

from __future__ import annotations

from vs_agent import AgentSessionKey, SessionScope

__all__ = ["AgentSessionKey", "SessionScope"]
