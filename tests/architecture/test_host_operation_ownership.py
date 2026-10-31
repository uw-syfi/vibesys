"""Shared host reads and failures survive removal of the helper adapter."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "owner", "retired_owner"),
    [
        ("ProjectLookupError", "server/host_examples.py", "server/host_helper_requests.py"),
        ("ComputeError", "server/host_compute.py", "server/host_helper_compute.py"),
        ("MachineCompute", "server/host_compute_runtime.py", "server/host_helper_compute.py"),
        ("ConfigFiles", "server/host_compute_runtime.py", "server/host_helper_compute.py"),
        ("OsConfigFiles", "server/host_compute_runtime.py", "server/host_helper_os.py"),
        ("ClusterCompute", "server/host_compute.py", "server/host_helper_requests.py"),
        ("AgentLibraryLogins", "server/host_agent_logins.py", "entrypoints/host_helper.py"),
        ("LoginStarter", "server/host_agent_logins.py", "server/host_helper_os.py"),
        ("HeldLogin", "server/host_agent_logins.py", "server/host_helper_os.py"),
        ("LoginBounds", "server/host_agent_logins.py", "server/host_helper_os.py"),
        ("HostReadSource", "server/host_operation_reads.py", "server/host_helper_requests.py"),
    ],
)
def test_shared_host_behavior_has_a_native_owner(name: str, owner: str, retired_owner: str) -> None:
    native = ast.parse((ROOT / "src" / owner).read_text())
    retired = ROOT / "src" / retired_owner
    legacy = ast.parse(retired.read_text() if retired.exists() else "")
    assert any(isinstance(node, ast.ClassDef) and node.name == name for node in native.body)
    assert not any(
        isinstance(node, ast.ClassDef) and node.name in {name, "HelperReadSource"}
        for node in legacy.body
    )


def _helper_dependencies(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            # ``from entrypoints import host_helper`` imports the module ``host_helper``.
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"import_module", "lazy_module"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            imported.add(node.args[0].value)
    return {
        module
        for module in imported
        if module.startswith(("server.host_helper", "entrypoints.host_helper"))
    }


def test_native_read_adapter_does_not_depend_on_the_helper() -> None:
    tree = ast.parse((ROOT / "src/server/host_operation_reads.py").read_text())
    assert not _helper_dependencies(tree)


def test_native_cluster_dispatch_does_not_depend_on_the_helper() -> None:
    tree = ast.parse((ROOT / "src/entrypoints/host_mutations.py").read_text())
    dispatch = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_serve_cluster"
    )
    assert not _helper_dependencies(dispatch)
    for path in ("src/server/host_compute_runtime.py", "src/server/host_slurm.py"):
        assert not _helper_dependencies(ast.parse((ROOT / path).read_text()))
    for path in ("src/server/host_helper_compute.py", "src/server/host_helper_slurm.py"):
        assert not (ROOT / path).exists(), f"retired helper owner returned: {path}"


def test_native_login_dispatch_does_not_depend_on_the_helper() -> None:
    tree = ast.parse((ROOT / "src/entrypoints/host.py").read_text())
    dispatch = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_serve_login"
    )
    watch_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and any(
            isinstance(part, ast.Name) and part.id == "_WATCH_COMMANDS"
            for part in ast.walk(node.test)
        )
    )
    assert not _helper_dependencies(dispatch)
    assert not _helper_dependencies(watch_branch)
    assert not _helper_dependencies(
        ast.parse((ROOT / "src/server/host_agent_logins.py").read_text())
    )


def test_transport_selection_cannot_return_to_clients() -> None:
    """The retained protobuf descriptor owns the old JSON key; clients have no switch."""
    selection = re.compile(r"transportv2|runtransportv2|transport_v2", re.IGNORECASE)
    violations = []
    for area in ("apps", "features", "libs"):
        for path in (ROOT / "clients" / area).rglob("*"):
            if not path.is_file() or path.suffix not in {".ts", ".tsx", ".cts", ".mjs", ".md"}:
                continue
            if set(path.parts) & {"node_modules", "dist", ".turbo"}:
                continue
            violations.extend(
                f"{path.relative_to(ROOT)}: {line.strip()}"
                for line in path.read_text().splitlines()
                if selection.search(line)
            )
    assert not violations, "\n".join(violations)
