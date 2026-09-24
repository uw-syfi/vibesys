"""VibeSys's feature flag manifest.

The reusable flag machinery lives in the local ``vs-feature-flags`` package.
Declare VibeSys-specific flags here and add their definitions to ``FEATURES``.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from vs_feature_flags.api import FeatureDefinition, FeatureRegistry


class FeatureFlag(StrEnum):
    """VibeSys-specific feature switches."""

    EXAMPLE_FEATURE = "example_feature"


FEATURES = FeatureRegistry(
    FeatureFlag,
    {
        FeatureFlag.EXAMPLE_FEATURE: FeatureDefinition(
            description="Exercise VibeSys feature flag plumbing.",
            default=False,
        ),
    },
)


def is_feature_enabled(
    flag: FeatureFlag,
    config: object | None = None,
) -> bool:
    """Resolve whether ``flag`` is enabled by the optional configuration."""
    overrides = _feature_flag_overrides(config)
    return FEATURES.is_enabled(flag, overrides)


def _feature_flag_overrides(config: object | None) -> Mapping[FeatureFlag, bool]:
    if config is None:
        return {}

    raw_overrides = getattr(config, "feature_flags", None)
    if raw_overrides is None and isinstance(config, Mapping):
        raw_overrides = config.get("feature_flags", {})
    if raw_overrides is None:
        raw_overrides = {}
    if not isinstance(raw_overrides, Mapping):
        message = "config.feature_flags must be a mapping"
        raise ValueError(message)  # noqa: TRY004  # lint-waiver: LW-010201 [TRY004]; callers and configuration validation tests rely on ValueError for malformed feature settings.

    return raw_overrides
