"""Evolutionary-search orchestration plugin."""

from typing import TYPE_CHECKING

from vibesys.orchestration.evolve.models import resolve_openevolve_options

if TYPE_CHECKING:
    from vibesys.orchestration.evolve.plugin import PLUGIN, REGISTRATION

_LAZY = {"PLUGIN", "REGISTRATION"}


def __getattr__(name: str) -> object:
    """Load execution wiring only when requested, keeping value and shell imports acyclic."""
    if name in _LAZY:
        # Eager wiring imports create a cycle through the execution shell;
        # importlib would hide the declared dependency behind runtime loading.
        from vibesys.orchestration.evolve.plugin import (  # noqa: PLC0415  # lint-waiver: LW-641108 [PLC0415]; eager wiring creates a shell cycle; importlib hides the declared dependency.
            PLUGIN,
            REGISTRATION,
        )

        return {"PLUGIN": PLUGIN, "REGISTRATION": REGISTRATION}[name]
    raise AttributeError(name)


__all__ = ["PLUGIN", "REGISTRATION", "resolve_openevolve_options"]
