"""The host's last good readings of provider limits, in ``limits.pb`` in its usage state directory.

``FileLimitsStore`` is ``server.usage_limits.LimitsStore`` over the file: written atomically, and a
file that is missing, does not decode, fails its rules or is another version is no readings, so the
next read rebuilds it. ``read_here`` and ``reader_here`` are the host's wiring of the policy: the
state directory beside the price list, and the reader that starts each provider's CLI in the
environment a run finds it in.

Light like the helper: the standard library, ``vs_project.api`` and the light ``server`` modules.

Public surface: ``FileLimitsStore``, ``read_here`` and ``reader_here``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.host_agent_logins import launch_environment
from server.usage_limits import (
    LIMITS_FILE,
    LIMITS_VERSION,
    PROBE_TIMEOUT_S,
    LimitsReader,
    collect,
)
from vs_agent.api.usage_limits import AgentLimitsReader
from vs_agent.api.usage_limits import providers as readable_providers
from vs_project.api import Project, ProjectError, atomic_write_bytes
from vs_proto.api import ValidationError, host_payloads, usage_state, validate

if TYPE_CHECKING:
    from pathlib import Path


class FileLimitsStore:
    """``LimitsStore`` in ``limits.pb``, written atomically."""

    def __init__(self, path: Path) -> None:
        """Keep the readings at ``path``."""
        self._path = path

    def read(self) -> usage_state.UsageLimitsState:
        """The stored readings; a missing, undecodable or invalid file is no readings."""
        try:
            found = usage_state.UsageLimitsState.from_binary(self._path.read_bytes())
            validate(found)
        except (OSError, ValueError, ValidationError):
            return usage_state.UsageLimitsState(version=LIMITS_VERSION)
        if found.version != LIMITS_VERSION:
            return usage_state.UsageLimitsState(version=LIMITS_VERSION)
        return found

    def write(self, state: usage_state.UsageLimitsState) -> None:
        """Replace the file, creating its directory; an I/O error loses only the fallback."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_bytes(self._path, state.to_binary())
        except (OSError, ProjectError):
            return


def read_here(
    request: host_payloads.UsageLimitsRequest, reader: LimitsReader
) -> host_payloads.UsageLimitsResponse:
    """The reply to ``request`` for every provider that has limits, kept in the host's usage state."""
    store = FileLimitsStore(Project.usage_state_directory() / LIMITS_FILE)
    return collect(request, reader, store, readable_providers())


def reader_here() -> LimitsReader:
    """The reader for this machine: each provider's CLI, found where a run finds it."""
    return AgentLimitsReader(launch_environment(), PROBE_TIMEOUT_S)


__all__ = ["FileLimitsStore", "read_here", "reader_here"]
