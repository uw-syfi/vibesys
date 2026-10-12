"""A world that cannot be copied is refused up front, by name."""

from __future__ import annotations

import pytest

from vs_dst.api import WorldNotCopyableError, simulate

from ._tiny import TinyWorld


class LockedWorld(TinyWorld):
    """A world whose position lives in a generator, which no copy can carry."""

    def __init__(self) -> None:
        super().__init__(1)
        self.replies = (reply for reply in range(3))


def test_a_world_that_cannot_be_deep_copied_fails_with_an_error_naming_its_type() -> None:
    with pytest.raises(WorldNotCopyableError, match=r"LockedWorld.*cannot be deep-copied"):
        simulate(LockedWorld())
