"""Static configuration for the profile-guided focus policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfileFocusConfig:
    """Plateau and improvement thresholds for one profiled component."""

    plateau_min_rounds: int = 2
    min_relative_improvement: float = 0.02

    def __post_init__(self) -> None:
        """Reject configuration that cannot express a valid schedule."""
        if self.plateau_min_rounds < 1:
            raise ValueError("plateau_min_rounds must be positive")  # noqa: TRY003
        if self.min_relative_improvement < 0:
            raise ValueError("min_relative_improvement must be non-negative")  # noqa: TRY003
