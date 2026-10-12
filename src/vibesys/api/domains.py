"""The modalities the registered domains declare, for frontends that offer them as choices."""

from __future__ import annotations

from vibesys.domains.registry import DOMAINS


def domain_modalities() -> tuple[str, ...]:
    """Every modality any registered domain declares, in registry order, without repeats."""
    return tuple(
        dict.fromkeys(
            modality for definition in DOMAINS.values() for modality in definition.modalities
        )
    )
