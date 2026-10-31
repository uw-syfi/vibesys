"""The catalogs this checkout offers a host: its vetted harnesses and its example projects.

Both the host helper channel and ``vibesys host <read>`` answer from them. The agent library
and the example machinery load on first use, so a process that never asks for a catalog does
not import them.

Public surface: ``vetted_harnesses`` and ``checkout_example_projects``.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from server.host_api import HarnessOption
from server.host_harness import HarnessDisplay, Maker, ModelGeneration
from vs_project.api import Git
from vs_sim.api import OsProcesses

if TYPE_CHECKING:
    from server.host_helper_requests import ExampleProjects
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


@cache
def checkout_example_projects() -> ExampleProjects:
    """This checkout's example catalog and project maker; loaded on first use."""
    # lint-waiver: LW-271505 [PLC0415]; the helper answers hello without loading the example machinery.
    # > `entrypoints.examples` brings argparse, configparser and the catalog reader; at module
    # > level every channel would load them before `hello`, past the helper's module ceiling
    # > (`tests/e2e/test_host_helper.py`). Moving the machinery into the helper would split the
    # > one implementation `vibesys example` uses, and the same deferral serves `harnesses`.
    from entrypoints.examples import GitExampleMaterializer, HelperExampleProjects  # noqa: PLC0415

    return HelperExampleProjects(source_checkout_root(), GitExampleMaterializer(Git(OsProcesses())))
