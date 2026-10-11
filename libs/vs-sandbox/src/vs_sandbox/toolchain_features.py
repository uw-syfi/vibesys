"""The catalog of language and container toolchains an image may include, as Features.

Each toolchain is a Dev Container Feature under ``features/src/`` that installs
everything it uses. This module owns the toolchain names, their version pins,
and the choice of Feature options; it is shared until tasks declare their own
toolchains. A caller selects names and gets :class:`~vs_sandbox.feature_image.LocalFeature`
values to pass to :func:`~vs_sandbox.feature_image.build_feature_image`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.feature_image import LocalFeature

if TYPE_CHECKING:
    from collections.abc import Collection

#: The folder holding ``src/<id>/`` and ``test/<id>/`` for every catalog Feature,
#: the layout ``devcontainer features test`` expects.
TOOLCHAIN_FEATURES_PROJECT = Path(__file__).resolve().parent / "features"

RUST_TOOLCHAIN = "rust"
GO_TOOLCHAIN = "go"
CONTAINER_RUNTIME_TOOLCHAIN = "container-runtime"
"""Toolchain name that adds a Docker engine, the compose plugin, kind, and
kubectl, for tasks whose candidate is a container topology."""

RUST_TOOLCHAIN_VERSION = "1.92.0"
"""Rust toolchain pin for the ``rust`` toolchain."""

GO_TOOLCHAIN_VERSION = "1.23.12"
"""Go toolchain pin for the ``go`` toolchain.

Mirrors the Go pin ``run_environment``'s evaluator setup already downloads at
run time.
"""

DOCKER_ENGINE_VERSION = "27.5.1"
"""Static Docker engine release (dockerd, containerd, runc, docker CLI) for the
``container-runtime`` toolchain; the daemon inside a Sysbox sandbox container."""

DOCKER_COMPOSE_VERSION = "2.32.4"
"""Docker Compose plugin release for the ``container-runtime`` toolchain."""

KIND_VERSION = "0.26.0"
"""kind release for the ``container-runtime`` toolchain."""

KUBECTL_VERSION = "1.32.1"
"""kubectl release for the ``container-runtime`` toolchain."""

#: Toolchain names in install order, least often changed first.
KNOWN_TOOLCHAINS: tuple[str, ...] = (RUST_TOOLCHAIN, GO_TOOLCHAIN, CONTAINER_RUNTIME_TOOLCHAIN)

_FEATURE_IDS = {
    RUST_TOOLCHAIN: "vibesys-rust",
    GO_TOOLCHAIN: "vibesys-go",
    CONTAINER_RUNTIME_TOOLCHAIN: "vibesys-container-runtime",
}


def toolchain_features(
    toolchains: Collection[str], *, docker_group_users: Collection[str] = ()
) -> tuple[LocalFeature, ...]:
    """Return the Features for *toolchains*, deduplicated and in catalog order.

    ``docker_group_users`` names users the container-runtime Feature adds to the
    ``docker`` group, which owns the daemon socket. Those users must already
    exist when the Feature runs (the Feature fails loudly if not), so a caller
    lists the Feature that creates them first.

    Raises:
        ValueError: A name is not in :data:`KNOWN_TOOLCHAINS`.
    """
    selected = set(toolchains)
    unknown = selected - set(KNOWN_TOOLCHAINS)
    if unknown:
        message = (
            f"unknown toolchain {sorted(unknown)!r}; the catalog provides "
            f"{sorted(KNOWN_TOOLCHAINS)!r}"
        )
        raise ValueError(message)
    options = {
        RUST_TOOLCHAIN: {"rust_version": RUST_TOOLCHAIN_VERSION},
        GO_TOOLCHAIN: {"go_version": GO_TOOLCHAIN_VERSION},
        CONTAINER_RUNTIME_TOOLCHAIN: {
            "docker_engine_version": DOCKER_ENGINE_VERSION,
            "docker_compose_version": DOCKER_COMPOSE_VERSION,
            "kind_version": KIND_VERSION,
            "kubectl_version": KUBECTL_VERSION,
            "users": " ".join(sorted(set(docker_group_users))),
        },
    }
    return tuple(
        LocalFeature(TOOLCHAIN_FEATURES_PROJECT / "src" / _FEATURE_IDS[name], options=options[name])
        for name in KNOWN_TOOLCHAINS
        if name in selected
    )
