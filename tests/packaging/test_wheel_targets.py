"""Contracts for the native release-wheel target matrix."""

from __future__ import annotations

import pytest
from wheel_targets import (
    TARGETS,
    WheelTargetError,
    resolve_wheel_target,
)

EXPECTED_PLATFORMS = {
    "linux-x86_64": "manylinux_2_28_x86_64",
    "linux-aarch64": "manylinux_2_28_aarch64",
    "macos-arm64": "macosx_13_0_arm64",
}

EXPECTED_BUN_SHA256 = {
    "linux-x86_64": "104d4d037f4b35e10215c0507e1779691f39c57bd91ddeefe11cad781e3fc4b9",
    "linux-aarch64": "a2c2862bcc1fd1c0b3a8dcdc8c7efb5e2acd871eb20ed2f17617884ede81c844",
    "macos-arm64": "cde6a4edf19cf64909158fa5a464a12026fd7f0d79a4a950c10cf0af04266d85",
}


@pytest.mark.parametrize("key", EXPECTED_PLATFORMS)
def test_supported_target_round_trip(key: str) -> None:
    target = TARGETS[key]

    resolved = resolve_wheel_target(
        key,
        host_system=target.system,
        host_machine=target.machine,
    )

    assert resolved == target
    assert resolved.wheel_platform == EXPECTED_PLATFORMS[key]
    assert resolved.opentui_package.startswith("@opentui/core-")
    assert resolved.bun_asset.endswith(".zip")
    assert resolved.bun_sha256 == EXPECTED_BUN_SHA256[key]


def test_target_resolution_rejects_an_unknown_target() -> None:
    with pytest.raises(WheelTargetError, match="Unsupported wheel target"):
        resolve_wheel_target("windows-x86_64", host_system="Windows", host_machine="AMD64")


def test_target_resolution_rejects_cross_compilation() -> None:
    with pytest.raises(WheelTargetError, match="must be built natively"):
        resolve_wheel_target(
            "linux-aarch64",
            host_system="Linux",
            host_machine="x86_64",
        )
