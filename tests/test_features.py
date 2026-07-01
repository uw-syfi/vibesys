from types import SimpleNamespace

import pytest

from vibe_serve import features
from vibe_serve.features import FEATURES, FeatureFlag, _feature_flag_overrides, is_feature_enabled


def test_vibeserve_has_no_declared_feature_flags_yet():
    assert list(FeatureFlag) == []
    assert FEATURES.definitions == {}


def test_is_feature_enabled_delegates_to_registry(monkeypatch):
    sentinel_flag = object()
    sentinel_overrides = object()

    class StubRegistry:
        def is_enabled(self, flag, overrides):
            assert flag is sentinel_flag
            assert overrides is sentinel_overrides
            return True

    monkeypatch.setattr(features, "FEATURES", StubRegistry())
    monkeypatch.setattr(features, "_feature_flag_overrides", lambda config: sentinel_overrides)

    assert is_feature_enabled(sentinel_flag, object()) is True


def test_feature_flag_overrides_defaults_to_empty_mapping():
    assert _feature_flag_overrides(None) == {}
    assert _feature_flag_overrides(SimpleNamespace(feature_flags=None)) == {}


def test_feature_flag_overrides_accepts_config_attribute():
    overrides = {FeatureFlag: True}

    assert _feature_flag_overrides(SimpleNamespace(feature_flags=overrides)) is overrides


def test_feature_flag_overrides_accepts_mapping_config():
    overrides = {FeatureFlag: False}

    assert _feature_flag_overrides({"feature_flags": overrides}) is overrides
    assert _feature_flag_overrides({}) == {}


def test_feature_flag_overrides_rejects_non_mapping_overrides():
    with pytest.raises(ValueError, match="config.feature_flags must be a mapping"):
        _feature_flag_overrides(SimpleNamespace(feature_flags=True))
