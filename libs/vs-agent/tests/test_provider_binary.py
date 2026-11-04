"""Each vetted provider names the program its CLI runs as."""

from __future__ import annotations

import pytest

from vs_agent.api import harnesses, provider_binary


@pytest.mark.parametrize("provider", sorted({harness.provider for harness in harnesses()}))
def test_every_vetted_provider_names_its_cli_program(provider: str) -> None:
    binary = provider_binary(provider)
    assert binary
    assert "/" not in binary


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="not-a-provider"):
        provider_binary("not-a-provider")
