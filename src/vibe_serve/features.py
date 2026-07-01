"""VibeServe's feature flag manifest.

The reusable flag machinery lives in the local ``vs-feature-flags`` package.
Declare VibeServe-specific flags here and add their definitions to ``FEATURES``.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from vs_feature_flags import FeatureRegistry


class FeatureFlag(StrEnum):
    pass


FEATURES = FeatureRegistry(FeatureFlag, {})


def is_feature_enabled(
    flag: FeatureFlag,
    config: Mapping[str, object] | None = None,
) -> bool:
    overrides = _feature_flag_overrides(config)
    return FEATURES.is_enabled(flag, overrides)


def _feature_flag_overrides(config: Mapping[str, object] | None) -> Mapping[FeatureFlag, bool]:
    if config is None:
        return {}

    raw_overrides = config.get("feature_flags", {})
    if not isinstance(raw_overrides, Mapping):
        raise ValueError("config['feature_flags'] must be a mapping")

    return raw_overrides
