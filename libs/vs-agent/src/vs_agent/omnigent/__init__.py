"""Import-safe Omnigent provider metadata.

The implementation lives in :mod:`vs_agent.drivers.omnigent` and imports
the optional dependency only while creating or running a session.
"""

from __future__ import annotations

from vs_agent.omnigent.providers import (
    OMNIGENT_PROVIDER_EXECUTORS,
    OmnigentExecutorSpec,
    supported_providers,
)

__all__ = [
    "OMNIGENT_PROVIDER_EXECUTORS",
    "OmnigentExecutorSpec",
    "supported_providers",
]
