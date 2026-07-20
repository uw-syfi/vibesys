from __future__ import annotations

from pathlib import Path

from vs_sandbox import host_resource_importer
from vs_sandbox.host_resources import HostResource, HostResourceAccess


def test_importer_partitions_access_and_write_wins(tmp_path):
    workspace = tmp_path / "runs" / "current" / "workspace"
    readonly = tmp_path / "readonly"
    writable = tmp_path / "writable"
    for path in (workspace, readonly, writable):
        path.mkdir(parents=True)

    imports = host_resource_importer.prepare_host_resource_imports(
        workspace,
        (
            HostResource(readonly),
            HostResource(writable),
            HostResource(writable, HostResourceAccess.READ_WRITE),
        ),
        log=lambda _message: None,
    )

    assert imports.read_paths == (readonly,)
    assert imports.write_paths == (writable,)


def test_importer_rejects_relative_and_symlinked_workspace_ancestors(tmp_path):
    run_root = tmp_path / "runs"
    workspace = run_root / "current" / "workspace"
    workspace.mkdir(parents=True)
    alias = tmp_path / "run-alias"
    alias.symlink_to(run_root, target_is_directory=True)
    logs: list[str] = []

    imports = host_resource_importer.prepare_host_resource_imports(
        workspace,
        (
            HostResource(Path("relative/bin"), purpose="relative"),
            HostResource(alias, purpose="aliased run root"),
        ),
        log=logs.append,
    )

    assert imports == host_resource_importer.HostResourceImports()
    assert any("non-absolute relative" in message for message in logs)
    assert any("aliased run root" in message and "ancestor" in message for message in logs)
