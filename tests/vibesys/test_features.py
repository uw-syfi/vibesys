from types import SimpleNamespace

import pytest

from vibesys.features import FEATURES, FeatureFlag, is_feature_enabled


def test_vibesys_declares_example_feature() -> None:
    assert FeatureFlag.EXAMPLE_FEATURE in list(FeatureFlag)
    assert FeatureFlag.EXAMPLE_FEATURE.value == "example_feature"
    assert FEATURES.definitions[FeatureFlag.EXAMPLE_FEATURE].description
    assert FEATURES.definitions[FeatureFlag.EXAMPLE_FEATURE].default is False


def test_every_flag_has_a_definition_and_defaults_off() -> None:
    """New flags must ship a definition, and none may default on."""
    for flag in FeatureFlag:
        definition = FEATURES.definitions[flag]
        assert definition.description
        assert definition.default is False


def test_is_feature_enabled_uses_manifest_default() -> None:
    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE) is False


@pytest.mark.parametrize("enabled", [False, True])
def test_is_feature_enabled_uses_config_object_override(enabled: object) -> None:
    config = SimpleNamespace(feature_flags={FeatureFlag.EXAMPLE_FEATURE: enabled})

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is enabled


def test_is_feature_enabled_treats_missing_config_overrides_as_empty() -> None:
    config = SimpleNamespace(feature_flags=None)

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is False


def test_is_feature_enabled_accepts_mapping_config() -> None:
    config = {"feature_flags": {FeatureFlag.EXAMPLE_FEATURE: True}}

    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, config) is True


def test_is_feature_enabled_treats_missing_mapping_overrides_as_empty() -> None:
    assert is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, {}) is False


def test_is_feature_enabled_rejects_non_mapping_overrides() -> None:
    with pytest.raises(ValueError, match=r"config\.feature_flags must be a mapping"):
        is_feature_enabled(FeatureFlag.EXAMPLE_FEATURE, SimpleNamespace(feature_flags=True))
