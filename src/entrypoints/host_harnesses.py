"""The harnesses the agent library vets, as the host helper channel and ``list-harnesses`` offer them.

The agent library loads on first use, so a process that never asks for the catalog does not
import it.

Public surface: ``vetted_harnesses``.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from server.host_api import HarnessOption
from server.host_harness import HarnessDisplay, Maker, ModelGeneration

if TYPE_CHECKING:
    from vs_agent.api import ModelDisplay


def _maker(vendor: str) -> Maker | None:
    """The maker agentshim's ``vendor`` names, or ``None`` for a vendor this contract lacks."""
    return next((maker for maker in Maker if maker.value == vendor), None)


def _generation(shown: ModelDisplay | None) -> ModelGeneration | None:
    """The generation agentshim ranks the model in, or ``None`` when it names none."""
    if shown is None or shown.generation is None:
        return None
    return ModelGeneration(
        id=shown.generation.id, name=shown.generation.name, order=shown.generation.rank
    )


@cache
def vetted_harnesses() -> tuple[HarnessOption, ...]:
    """The agent library's vetted harnesses, each provider's default marked; loaded on first use."""
    # lint-waiver: LW-271503 [PLC0415]; the helper answers hello without loading the agent library.
    # > `vs_agent.api` owns the vetted list and takes about half a second to import; at module
    # > level every channel would pay for it before `hello` (the light-entrypoint guards forbid
    # > it). A copy of the list in a light module would be a second source of truth, and a
    # > generated copy would need a drift check for what one deferred import already gives.
    from vs_agent.api import (  # noqa: PLC0415
        default_model,
        harnesses,
        model_display,
        provider_display_name,
    )

    options: list[HarnessOption] = []
    for harness in harnesses():
        shown = model_display(harness.provider, harness.model)
        options.append(
            HarnessOption(
                provider=harness.provider,
                model=harness.model,
                default=default_model(harness.provider) == harness.model,
                display=HarnessDisplay(
                    provider_name=provider_display_name(harness.provider),
                    model_name=None if shown is None else shown.model_name,
                    family=None if shown is None else shown.family,
                    tier=None if shown is None else shown.tier,
                    tier_name=None if shown is None else shown.tier_name,
                    maker=None if shown is None else _maker(shown.vendor),
                    generation=_generation(shown),
                ),
            )
        )
    return tuple(options)
