"""Native target definitions for self-contained VibeSys release wheels."""

from __future__ import annotations

from dataclasses import dataclass


class WheelTargetError(ValueError):
    """Raised when a release target is unknown or built on the wrong host."""

    @classmethod
    def unsupported(cls, key: str) -> WheelTargetError:
        """Build an error for an unknown matrix key."""
        return cls(f"Unsupported wheel target: {key}")

    @classmethod
    def non_native(
        cls,
        key: str,
        *,
        expected_system: str,
        expected_machine: str,
        host_system: str,
        host_machine: str,
    ) -> WheelTargetError:
        """Build an error for a target that does not match the current host."""
        return cls(
            f"{key} must be built natively on {expected_system}/{expected_machine}, "
            f"not {host_system}/{host_machine}"
        )


@dataclass(frozen=True)
class WheelTarget:
    """One supported native wheel and its bundled runtime assets."""

    key: str
    system: str
    machine: str
    wheel_platform: str
    opentui_package: str
    bun_asset: str
    bun_sha256: str


TARGETS: dict[str, WheelTarget] = {
    "linux-x86_64": WheelTarget(
        key="linux-x86_64",
        system="Linux",
        machine="x86_64",
        wheel_platform="manylinux_2_28_x86_64",
        opentui_package="@opentui/core-linux-x64",
        bun_asset="bun-linux-x64-baseline.zip",
        bun_sha256="c678040f14fe0440eb839d37cbd0ce4c051a32da72806ac97de6a6aab6bf728f",
    ),
    "linux-aarch64": WheelTarget(
        key="linux-aarch64",
        system="Linux",
        machine="aarch64",
        wheel_platform="manylinux_2_28_aarch64",
        opentui_package="@opentui/core-linux-arm64",
        bun_asset="bun-linux-aarch64.zip",
        bun_sha256="54328bbc2d9c8e0c9f892c544d66c57a83b84139e34909e5ee81758f1ac8fda7",
    ),
    "macos-arm64": WheelTarget(
        key="macos-arm64",
        system="Darwin",
        machine="arm64",
        wheel_platform="macosx_13_0_arm64",
        opentui_package="@opentui/core-darwin-arm64",
        bun_asset="bun-darwin-aarch64.zip",
        bun_sha256="90987a3a16d7db556d886ac3d551e7b6d3edf0a1cf43acaed622e8676be1d12f",
    ),
}


def resolve_wheel_target(
    key: str,
    *,
    host_system: str,
    host_machine: str,
) -> WheelTarget:
    """Return a supported target after proving this is its native host."""
    try:
        target = TARGETS[key]
    except KeyError as exc:
        raise WheelTargetError.unsupported(key) from exc
    if (host_system, host_machine) != (target.system, target.machine):
        error = WheelTargetError.non_native(
            key,
            expected_system=target.system,
            expected_machine=target.machine,
            host_system=host_system,
            host_machine=host_machine,
        )
        raise error
    return target
