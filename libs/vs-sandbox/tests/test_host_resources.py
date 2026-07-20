from __future__ import annotations

from pathlib import Path

from vs_sandbox import (
    HostResource,
    HostResourceAccess,
    HostResourceContext,
    declare_resources,
)


def test_declaration_sdk_collects_resources_without_importing_them(tmp_path):
    toolchain = tmp_path / "toolchain"
    context = HostResourceContext(env={"PROFILE": "test"})

    def declare(ctx: HostResourceContext):
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
