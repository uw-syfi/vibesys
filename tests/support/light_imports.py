"""What the light host commands may not import: shared by the static and real guards.

``tests/architecture/test_light_entrypoints.py`` checks the static import
closure of each light entrypoint; ``tests/e2e/test_light_host_commands.py``
checks what a real process loads (as does ``tests/e2e/test_host_helper.py``).
They name the heavy layers from this one list, in groups, so a failure names the layer a
module belongs to as well as the module.

A module list is only as complete as its source. ``-X importtime`` does not report a module
loaded through ``importlib`` (a ``lazy_module`` handle, or a package that loads its submodules
on first use, as Pydantic and ``vs_sim`` do), so a real-process check that must see every
module reads ``sys.modules`` in the process.
"""

from __future__ import annotations

import importlib.util
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class Layer(StrEnum):
    """A heavy layer no light host command has a reason to load."""

    ENGINE = "the run engine and its transport"
    AGENT_DRIVERS = "the agent drivers and what they load"
    PROVIDER_SDKS = "provider SDKs and hosted-service clients"
    HEAVY_LIBRARIES = "web, async, numerical and plotting libraries"


HEAVY: dict[Layer, tuple[str, ...]] = {
    Layer.ENGINE: (
        "vibesys",
        "vs_runtime",
        "vs_core",
        "server.api",
        "server.events",
        "server.chat",
        "server.runtime",
        "server.transport",
    ),
    Layer.AGENT_DRIVERS: (
        "vs_agent",
        "agentshim",
        "vs_prompts",
        "vs_sandbox",
        "vs_mcp",
        "mcp",
        "jinja2",
    ),
    Layer.PROVIDER_SDKS: ("openai", "huggingface_hub", "modal"),
    Layer.HEAVY_LIBRARIES: (
        "websockets",
        "aiohttp",
        "anyio",
        "httpx",
        "requests",
        "starlette",
        "uvicorn",
        "numpy",
        "matplotlib",
        "networkx",
        "PIL",
        "rich",
    ),
}
"""Each layer's modules, by name; a package name stands for everything under it."""

DENIED_HEAVY = tuple(module for modules in HEAVY.values() for module in modules)
"""Modules (and everything under them) that load the run engine, an agent or a transport."""


def under(name: str, roots: Iterable[str]) -> bool:
    """Whether module ``name`` is, or is under, one of ``roots`` (a package stands for its tree)."""
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def is_heavy(name: str) -> bool:
    """Whether module ``name`` is, or is under, one of ``DENIED_HEAVY``."""
    return under(name, DENIED_HEAVY)


def offenders[Group: str](
    modules: Iterable[str], groups: Mapping[Group, Iterable[str]]
) -> dict[Group, list[str]]:
    """The loaded ``modules`` each group of ``groups`` forbids, sorted; empty groups left out.

    A module under two groups is reported under both, so a failure never hides which layer an
    import crossed into.
    """
    loaded = sorted(set(modules))
    found = {
        group: [name for name in loaded if under(name, roots)] for group, roots in groups.items()
    }
    return {group: names for group, names in found.items() if names}


def describe[Group: str](found: Mapping[Group, Iterable[str]]) -> str:
    """``offenders``' result as one line: each group with the modules of it that were loaded."""
    return "; ".join(f"{group}: {', '.join(names)}" for group, names in found.items())


def missing_modules[Group: str](groups: Mapping[Group, Iterable[str]]) -> list[str]:
    """The names in ``groups`` that no installed module has: a ban on a renamed module bans nothing."""
    missing: set[str] = set()
    for roots in groups.values():
        for root in roots:
            try:
                found = importlib.util.find_spec(root) is not None
            except ModuleNotFoundError:
                found = False
            if not found:
                missing.add(root)
    return sorted(missing)


BUDGET_MS = 300
"""The start-up budget for a light host command on a development machine."""


def loaded_modules(importtime_stderr: str) -> list[str]:
    """Module names from ``-X importtime`` lines (``import time: self | cumulative | name``).

    This misses a module loaded through ``importlib`` (see the module docstring) and lists a
    module that failed to import; read ``sys.modules`` where a check must see every module.
    """
    return [
        line.rsplit("|", 1)[1].strip()
        for line in importtime_stderr.splitlines()
        if line.startswith("import time:") and "|" in line and "imported package" not in line
    ]
