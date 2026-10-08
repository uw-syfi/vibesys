"""The Hypothesis profiles registered in the root ``conftest.py``."""

from __future__ import annotations

import pytest
from hypothesis import settings


@pytest.mark.parametrize("name", ["dev", "ci", "explore", "nightly"])
def test_no_profile_uses_a_wall_clock_deadline(name: str) -> None:
    assert settings.get_profile(name).deadline is None


@pytest.mark.parametrize("name", ["ci", "explore", "nightly"])
def test_profiles_that_share_failures_name_the_example_database(name: str) -> None:
    """``derandomize=True`` would force ``database=None``, so no profile uses it."""
    profile = settings.get_profile(name)

    assert profile.derandomize is False
    assert profile.database is not None
