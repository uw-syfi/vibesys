"""Contracts for selecting the release wheel installation scheme."""

from __future__ import annotations

import pytest
from packaging_support import release_has_native_payload


@pytest.mark.parametrize(
    "target",
    [None, "linux-x86_64"],
)
def test_distribution_is_non_pure_only_for_targeted_release_builds(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
) -> None:
    if target is None:
        monkeypatch.delenv("VIBESYS_WHEEL_TARGET", raising=False)
    else:
        monkeypatch.setenv("VIBESYS_WHEEL_TARGET", target)

    assert release_has_native_payload() is (target is not None)
