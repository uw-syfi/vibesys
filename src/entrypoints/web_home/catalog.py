"""The launch options the start form offers: drivers, providers, loops, backends."""

from __future__ import annotations

import platform
import shutil
import sys
from typing import TYPE_CHECKING

from entrypoints.cli.args import _build_agent_parser, _build_evolve_parser, _build_plain_parser
from entrypoints.cli.constants import _OUTER_LOOPS
from entrypoints.web_home.contract import (
    Catalog,
    DriverOption,
    LoopBudget,
    OuterLoopOption,
    ProviderOption,
)
from vibesys.api import SUGGESTED_MODELS, ComputeBackend
from vibesys.api.request import orchestration_roles
from vs_agent.api import SHIPPED_PROVIDERS, agent_catalog, provider_profile

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable
    from typing import Literal

    from entrypoints.web_home.context import Request

    BudgetFlag = Literal["--max-rounds", "--max-generations"]

# Outer loop -> (total-budget flag, parser that owns its default, orchestration whose roles
# the form offers). profile-guided and dynamic parse with the agent parser, so they take
# --max-rounds too (`entrypoints/cli/__init__.py` loop table).
LOOPS: dict[str, tuple[BudgetFlag, Callable[[], argparse.ArgumentParser], str]] = {
    "agent": ("--max-rounds", _build_agent_parser, "multi-agent"),
    "profile-guided": ("--max-rounds", _build_agent_parser, "profile-guided-multi-agent"),
    "dynamic": ("--max-rounds", _build_agent_parser, "dynamic"),
    "plain": ("--max-rounds", _build_plain_parser, "plain"),
    "evolve": ("--max-generations", _build_evolve_parser, "evolve"),
}


def budget_destination(flag: str) -> str:
    """Return the argparse destination (and descriptor option) of a budget flag."""
    return flag.removeprefix("--").replace("-", "_")


def host_compute_backend() -> ComputeBackend:
    """Return the compute backend this machine most likely has."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return ComputeBackend.METAL
    if shutil.which("nvidia-smi"):
        return ComputeBackend.CUDA
    if shutil.which("rocm-smi"):
        return ComputeBackend.ROCM
    return ComputeBackend.CPU


def _loop(loop_id: str) -> OuterLoopOption:
    flag, parser, orchestration = LOOPS[loop_id]
    return OuterLoopOption(
        id=loop_id,
        budget=LoopBudget(flag=flag, default=parser().get_default(budget_destination(flag))),
        requires_profile_guided=loop_id == "profile-guided",
        roles=list(orchestration_roles(orchestration)),
    )


def _provider(name: str) -> ProviderOption:
    profile = provider_profile(name)
    return ProviderOption(
        provider=name,
        display_name=profile.display_name,
        supports_reasoning_effort=profile.supports_reasoning_effort,
        suggested_models=list(SUGGESTED_MODELS.get(name, ())),
    )


def get_catalog(request: Request) -> Catalog:
    """``GET /api/agents/catalog``."""
    del request
    return Catalog(
        drivers=[
            DriverOption(
                driver=driver, providers=list(info.providers), supports_docker=info.supports_docker
            )
            for driver, info in agent_catalog().items()
        ],
        providers=[_provider(name) for name in SHIPPED_PROVIDERS],
        outer_loops=[_loop(loop_id) for loop_id in _OUTER_LOOPS],
        compute_backends=list(ComputeBackend),
        default_compute_backend=host_compute_backend(),
    )
