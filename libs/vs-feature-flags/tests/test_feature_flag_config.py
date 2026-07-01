from enum import StrEnum

import pytest

from vs_feature_flags import parse_feature_flag_overrides


class ExampleFlag(StrEnum):
    NEW_LOOP = "new_loop"
    STRICT_MODE = "strict_mode"


def test_parse_feature_flag_overrides_returns_typed_flags():
    parsed = parse_feature_flag_overrides({"new_loop": True}, ExampleFlag)

    assert parsed == {ExampleFlag.NEW_LOOP: True}


def test_parse_feature_flag_overrides_accepts_missing_section():
    assert parse_feature_flag_overrides(None, ExampleFlag) == {}


def test_parse_feature_flag_overrides_rejects_non_table():
    with pytest.raises(ValueError, match="feature_flags must be a TOML table"):
        parse_feature_flag_overrides(True, ExampleFlag)


def test_parse_feature_flag_overrides_rejects_unknown_flag():
    with pytest.raises(ValueError, match="Unknown feature flag 'nope'"):
        parse_feature_flag_overrides({"nope": True}, ExampleFlag)


def test_parse_feature_flag_overrides_rejects_non_bool_value():
    with pytest.raises(ValueError, match="feature_flags.new_loop must be true or false"):
        parse_feature_flag_overrides({"new_loop": "true"}, ExampleFlag)
