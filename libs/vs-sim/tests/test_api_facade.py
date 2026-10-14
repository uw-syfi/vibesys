"""``vs_sim.api`` resolves every public name on first use, and nothing else."""

from __future__ import annotations

import pytest

import vs_sim.api


@pytest.mark.parametrize("name", vs_sim.api.__all__)
def test_every_public_name_resolves_and_stays_resolved(name: str) -> None:
    value = getattr(vs_sim.api, name)

    assert value is not None
    assert getattr(vs_sim.api, name) is value
    assert name in dir(vs_sim.api)


def test_an_unknown_name_is_an_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        _ = vs_sim.api.no_such_name
