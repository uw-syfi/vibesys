"""Headless tests and their children inherit test-owned GitHub state."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_github_auth(isolated_github_auth: None) -> None:
    """Use the shared empty GitHub environment for every headless test."""
