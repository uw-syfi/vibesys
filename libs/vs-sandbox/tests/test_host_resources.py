from __future__ import annotations

from pathlib import Path

from vs_sandbox.api import (
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    SandboxKind,
    declare_resources,
    deduplicate_host_resources,
    host_resource_for_mount,
)
from vs_sandbox.api.testing import FakeComputeBackend


def test_declaration_sdk_collects_resources_without_importing_them(tmp_path: Path) -> None:
    toolchain = tmp_path / "toolchain"
    context = HostResourceContext(env={"PROFILE": "test"})

    def declare(ctx: HostResourceContext) -> tuple[HostResource, ...]:
        assert ctx == context
        return (HostResource(toolchain, purpose="test toolchain"),)

    additional = HostResource(
        Path("/opt/model-cache"),
        HostResourceAccess.READ_WRITE,
        purpose="model cache",
    )

    assert declare_resources(
        context,
        (declare,),
        additional=(additional,),
    ) == (
        HostResource(toolchain, purpose="test toolchain"),
        additional,
    )


def test_mount_lowering_preserves_access_purpose_and_identity_mapping(tmp_path: Path) -> None:
    host_path = tmp_path / "cache"

    remapped = host_resource_for_mount(
        host_path,
        "/opt/cache",
        read_only=False,
        purpose="build cache",
    )
    identity = host_resource_for_mount(
        host_path,
        str(host_path),
        read_only=True,
    )

    assert remapped == HostResource(
        host_path,
        HostResourceAccess.READ_WRITE,
        "build cache",
        "/opt/cache",
    )
    assert identity == HostResource(host_path, HostResourceAccess.READ_ONLY, "container mount")


def test_later_resource_wins_without_reordering_the_visible_path(tmp_path: Path) -> None:
    first = HostResource(Path("/opt/tool"))
    unrelated = HostResource(tmp_path / "other", agent_path="/opt/other")
    replacement = HostResource(
        tmp_path / "new",
        HostResourceAccess.READ_WRITE,
        agent_path="/opt/tool",
    )

    assert deduplicate_host_resources((first, unrelated, replacement)) == (
        replacement,
        unrelated,
    )


def test_normalized_resources_flow_through_the_public_backend_fake(tmp_path: Path) -> None:
    replacement = host_resource_for_mount(
        tmp_path / "replacement",
        "/opt/tool",
        read_only=False,
    )
    resources = deduplicate_host_resources(
        (
            host_resource_for_mount(tmp_path / "original", "/opt/tool", read_only=True),
            replacement,
        )
    )
    backend = FakeComputeBackend()

    backend.make_sandbox(
        SandboxKind.DOCKER,
        host_workspace=str(tmp_path / "workspace"),
        resources=resources,
    )

    assert backend.creations[0].resources == (replacement,)
