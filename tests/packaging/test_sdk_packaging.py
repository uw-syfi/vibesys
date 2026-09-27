"""VibeSys composition contract for locating its bundled input SDK."""

from vibesys.constants import PROJECT_ROOT
from vibesys.sdk_paths import sdk_root


def test_sdk_root_prefers_the_repository_checkout() -> None:
    assert sdk_root() == PROJECT_ROOT / "sdk"
