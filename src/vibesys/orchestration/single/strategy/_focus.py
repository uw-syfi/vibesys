"""The profile-guided preset's component-focus policy and the attribution protocol it reads."""

from vibesys.orchestration.single.strategy._config import SingleConfig
from vibesys.profile_focus import ProfileFocus, ProfileFocusConfig

BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
END = "__VIBESYS_ATTRIBUTION_END__"
OUTPUT_ARGUMENT = "--vs-output"


def focus_policy(config: SingleConfig) -> ProfileFocus | None:
    """The component-focus policy of a profile-guided run, else None."""
    attribution = config.attribution
    if attribution is None:
        return None
    return ProfileFocus(
        ProfileFocusConfig(
            plateau_min_rounds=attribution.min_measured_rounds,
            min_relative_improvement=attribution.min_relative_improvement,
        )
    )
