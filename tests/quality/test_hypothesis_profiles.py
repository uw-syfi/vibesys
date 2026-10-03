"""The Hypothesis profiles registered in the root ``conftest.py``."""

from __future__ import annotations

import pytest
from hypothesis import settings


@pytest.mark.parametrize("name", ["dev", "ci", "explore"])
def test_no_profile_uses_a_wall_clock_deadline(name: str) -> None:
    assert settings.get_profile(name).deadline is None


def test_ci_profile_is_derandomized_and_explore_is_not() -> None:
    assert settings.get_profile("ci").derandomize is True
    assert settings.get_profile("explore").derandomize is False
