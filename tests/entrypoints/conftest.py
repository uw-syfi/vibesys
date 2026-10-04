"""Entrypoint tests own their GitHub configuration and credentials."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_github_auth(isolated_github_auth: None) -> None:
    """Use the shared empty GitHub environment for every entrypoint test."""
