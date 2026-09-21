"""The agent driver catalog: one query for what each driver supports.

Replaces the old ``supported_cli_providers(driver_name)`` function (which
callers had to pair with their own ``AGENT_DRIVERS`` tuple to even know which
driver names were valid) with a single mapping keyed by :class:`~vibesys.
agents.spec.Driver`. Each entry's provider list is resolved at call time from
the same source ``build_agent_client`` uses to actually run that driver, so
this can never drift from what a driver accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from vs_agent.spec import Driver

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class DriverInfo:
    """What one driver supports: which providers it can run, and how."""

    driver: Driver
    providers: tuple[str, ...]
    supports_docker: bool


def agent_catalog() -> Mapping[Driver, DriverInfo]:
    """Return every driver's supported providers and docker support.

    Provider lists are resolved live, not hardcoded, so a library upgrade
    that adds or removes a provider changes this without an edit here:

    - ``agentshim`` reads its own ``supported_providers()`` (backed by
      ``vs_agent.provider_policy.SHIPPED_PROVIDERS``).
    - ``omnigent`` reads its executor registry
      (``vs_agent.omnigent.providers.OMNIGENT_PROVIDER_EXECUTORS``).
    - ``mock`` accepts only its own label; it drives no CLI, so provider
      selection has nothing else to validate against.

    ``supports_docker`` mirrors ``build_agent_client``'s existing gating:
    Omnigent has no container-execution path and is rejected outright with
    ``--docker`` (see ``OmnigentDriverError``'s remedy pointing at
    ``agent.driver='agentshim'``); AgentShim and the mock have never gated on
    it.
    """
    from vs_agent.drivers.agentshim import (  # noqa: PLC0415  # avoid import cycle
        supported_providers as agentshim_providers,
    )
    from vs_agent.drivers.mock import (  # noqa: PLC0415  # avoid import cycle
        supported_providers as mock_providers,
    )
    from vs_agent.omnigent.providers import (  # noqa: PLC0415  # avoid import cycle
        supported_providers as omnigent_providers,
    )

    return MappingProxyType(
        {
            Driver.AGENTSHIM: DriverInfo(
                driver=Driver.AGENTSHIM,
                providers=tuple(agentshim_providers()),
                supports_docker=True,
            ),
            Driver.OMNIGENT: DriverInfo(
                driver=Driver.OMNIGENT,
                providers=tuple(omnigent_providers()),
                supports_docker=False,
            ),
            Driver.MOCK: DriverInfo(
                driver=Driver.MOCK,
                providers=tuple(mock_providers()),
                supports_docker=True,
            ),
        }
    )
