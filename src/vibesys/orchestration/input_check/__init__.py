"""Public surface for the gates-only input-check orchestration."""

from typing import TYPE_CHECKING

from vibesys.orchestration.input_check.launch import InputCheckLaunch

if TYPE_CHECKING:
    from vibesys.orchestration.input_check.plugin import PLUGIN, REGISTRATION


def __getattr__(name: str) -> object:
    """Load execution wiring only when the built-in catalog requests it."""
    if name in {"PLUGIN", "REGISTRATION"}:
        from vibesys.orchestration.input_check.plugin import (  # noqa: PLC0415  # lint-waiver: LW-209401 [PLC0415]; eager plugin wiring cycles through the built-in catalog.
            PLUGIN,
            REGISTRATION,
        )

        return PLUGIN if name == "PLUGIN" else REGISTRATION
    raise AttributeError(name)


__all__ = ["PLUGIN", "REGISTRATION", "InputCheckLaunch"]
