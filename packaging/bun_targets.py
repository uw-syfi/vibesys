"""Pinned official Bun artifacts for client test hosts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BunTarget:
    """One verified Bun archive for a supported host."""

    asset: str
    sha256: str


TARGETS: dict[str, BunTarget] = {
    "linux-x86_64": BunTarget(
        asset="bun-linux-x64-baseline.zip",
        sha256="c678040f14fe0440eb839d37cbd0ce4c051a32da72806ac97de6a6aab6bf728f",
    ),
    "linux-aarch64": BunTarget(
        asset="bun-linux-aarch64.zip",
        sha256="54328bbc2d9c8e0c9f892c544d66c57a83b84139e34909e5ee81758f1ac8fda7",
    ),
    "macos-arm64": BunTarget(
        asset="bun-darwin-aarch64.zip",
        sha256="90987a3a16d7db556d886ac3d551e7b6d3edf0a1cf43acaed622e8676be1d12f",
    ),
}
