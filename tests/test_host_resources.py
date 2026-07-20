from __future__ import annotations

from pathlib import Path

from vibesys import host_resources


def test_declaration_sdk_collects_resources_without_importing_them(tmp_path):
    toolchain = tmp_path / "toolchain"
    context = host_resources.HostResourceContext(env={"PROFILE": "test"})

    def declare(ctx: host_resources.HostResourceContext):
        assert ctx == context
        return (host_resources.HostResource(toolchain, purpose="test toolchain"),)

    additional = host_resources.HostResource(
        Path("/opt/model-cache"),
        host_resources.HostResourceAccess.READ_WRITE,
        purpose="model cache",
    )

    assert host_resources.declare_resources(
        context,
        (declare,),
        additional=(additional,),
    ) == (
        host_resources.HostResource(toolchain, purpose="test toolchain"),
        additional,
    )
