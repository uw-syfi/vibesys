"""Public surface of the single-agent orchestration policy slice."""

from typing import TYPE_CHECKING

from vibesys.orchestration.single.models import (
    ProfileGuidedSingleOptions,
    SingleAgentRoundResponse,
    SingleOptions,
)

if TYPE_CHECKING:
    from vibesys.orchestration.single.plugin import (
        PLUGIN,
        PROFILE_GUIDED_PLUGIN,
        PROFILE_GUIDED_REGISTRATION,
        REGISTRATION,
    )

_LAZY = {"PLUGIN", "PROFILE_GUIDED_PLUGIN", "PROFILE_GUIDED_REGISTRATION", "REGISTRATION"}


def __getattr__(name: str) -> object:
    """Load execution wiring only when requested, keeping value and shell imports acyclic."""
    if name in _LAZY:
        # Eager wiring imports create a cycle through the execution shell;
        # importlib would hide the declared dependency behind runtime loading.
        from vibesys.orchestration.single.plugin import (  # noqa: PLC0415  # lint-waiver: LW-641005 [PLC0415]; eager wiring creates a shell cycle; importlib hides the declared dependency.
            PLUGIN,
            PROFILE_GUIDED_PLUGIN,
            PROFILE_GUIDED_REGISTRATION,
            REGISTRATION,
        )

        return {
            "PLUGIN": PLUGIN,
            "PROFILE_GUIDED_PLUGIN": PROFILE_GUIDED_PLUGIN,
            "PROFILE_GUIDED_REGISTRATION": PROFILE_GUIDED_REGISTRATION,
            "REGISTRATION": REGISTRATION,
        }[name]
    raise AttributeError(name)


__all__ = [
    "PLUGIN",
    "PROFILE_GUIDED_PLUGIN",
    "PROFILE_GUIDED_REGISTRATION",
    "REGISTRATION",
    "ProfileGuidedSingleOptions",
    "SingleAgentRoundResponse",
    "SingleOptions",
]
