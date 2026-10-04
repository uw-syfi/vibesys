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
    "linux-x86_64": "c678040f14fe0440eb839d37cbd0ce4c051a32da72806ac97de6a6aab6bf728f",
    "linux-aarch64": "54328bbc2d9c8e0c9f892c544d66c57a83b84139e34909e5ee81758f1ac8fda7",
    "macos-arm64": "90987a3a16d7db556d886ac3d551e7b6d3edf0a1cf43acaed622e8676be1d12f",
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
