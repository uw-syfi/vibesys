from __future__ import annotations

import importlib.util


def test_old_nested_server_package_is_absent() -> None:
    legacy_package = "vibesys.server"
    assert importlib.util.find_spec(legacy_package) is None
