"""VibeServe's feature flag manifest.

The reusable flag machinery lives in the local ``vs-feature-flags`` package.
Declare VibeServe-specific flags here and add their definitions to ``FEATURES``.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from vs_feature_flags import FeatureDefinition, FeatureRegistry


class FeatureFlag(StrEnum):
    EXAMPLE_FEATURE = "example_feature"


FEATURES = FeatureRegistry(
    FeatureFlag,
    {
        FeatureFlag.EXAMPLE_FEATURE: FeatureDefinition(
            description="Exercise VibeServe feature flag plumbing.",
            default=False,
        ),
    },
)


def is_feature_enabled(
    flag: FeatureFlag,
    config: object | None = None,
) -> bool:
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
        raise ValueError("config.feature_flags must be a mapping")

    return raw_overrides
