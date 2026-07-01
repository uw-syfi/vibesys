from types import SimpleNamespace

import pytest

from vibe_serve.features import FEATURES, FeatureFlag, is_feature_enabled


def test_vibeserve_declares_example_feature():
    assert list(FeatureFlag) == [FeatureFlag.EXAMPLE_FEATURE]
    assert FeatureFlag.EXAMPLE_FEATURE.value == "example_feature"
    assert FEATURES.definitions[FeatureFlag.EXAMPLE_FEATURE].description
    assert FEATURES.definitions[FeatureFlag.EXAMPLE_FEATURE].default is False


def test_is_feature_enabled_uses_manifest_default():
    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE) is False


@pytest.mark.parametrize("enabled", [False, True])
def test_is_feature_enabled_uses_config_object_override(enabled):
    config = SimpleNamespace(feature_flags={FeatureFlag.EXAMPLE_FEATURE: enabled})

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is enabled


def test_is_feature_enabled_treats_missing_config_overrides_as_empty():
    config = SimpleNamespace(feature_flags=None)

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is False


def test_is_feature_enabled_accepts_mapping_config():
    config = {"feature_flags": {FeatureFlag.EXAMPLE_FEATURE: True}}

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is True


def test_is_feature_enabled_treats_missing_mapping_overrides_as_empty():
    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, {}) is False


def test_is_feature_enabled_rejects_non_mapping_overrides():
    with pytest.raises(ValueError, match="config.feature_flags must be a mapping"):
        is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, SimpleNamespace(feature_flags=True))
