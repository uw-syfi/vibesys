from vibe_serve.features import FEATURES, FeatureFlag


def test_vibeserve_has_no_declared_feature_flags_yet():
    assert list(FeatureFlag) == []
    assert FEATURES.definitions == {}
