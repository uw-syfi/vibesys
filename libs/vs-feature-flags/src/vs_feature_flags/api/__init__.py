"""Public feature flag definitions, lookup, and override parsing.

Define flags with ``FeatureDefinition`` and register them in a
``FeatureRegistry``. ``parse_feature_flag_overrides`` parses configuration
overrides against that registry, rejecting unknown names and invalid values.
"""

from vs_feature_flags.config import parse_feature_flag_overrides
from vs_feature_flags.core import FeatureDefinition, FeatureRegistry

__all__ = [
    "FeatureDefinition",
    "FeatureRegistry",
    "parse_feature_flag_overrides",
]
