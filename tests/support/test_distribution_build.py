"""Contracts for test-owned distribution source trees."""

from __future__ import annotations

import ast
import copy
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.distribution_build import (
    PrivatePackagingSource,
    build_distribution,
    copy_private_packaging_source,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_ROOT = Path(__file__).resolve().parents[2]
_OWNER = Path(__file__).with_name("distribution_build.py")


def _track_source(source: Path) -> None:
    run_test_command(["git", "init", "-q"], cwd=source, check=True)
    run_test_command(["git", "add", "--all"], cwd=source, check=True)


def test_private_source_preserves_working_bytes_and_excludes_generated_outputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "working"
    (source / "src" / "example").mkdir(parents=True)
    changed = source / "src" / "example" / "uncommitted.py"
    changed.write_text("VALUE = 0\n")
    (source / "vibesys-guide").mkdir(parents=True)
    (source / "vibesys-guide" / "README.md").write_text("source, not an sdist tree\n")
    _track_source(source)
    changed.write_text("VALUE = 1\n")
    for generated in (
        ".venv",
        "build",
        "dist",
        "example.egg-info",
        "node_modules",
        "vibesys-0.1.1",
    ):
        (source / generated).mkdir()
        (source / generated / "generated").write_text("not source\n")

    private = copy_private_packaging_source(source, tmp_path / "private")

    assert (private.root / "src" / "example" / "uncommitted.py").read_text() == "VALUE = 1\n"
    assert sorted(path.name for path in private.root.iterdir()) == ["src", "vibesys-guide"]


def test_private_source_cannot_be_nested_in_the_tree_it_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "working"
    source.mkdir()
    _track_source(source)

    with pytest.raises(ValueError, match="outside the source tree"):
        copy_private_packaging_source(source, source / "build-copy")


def test_snapshot_and_artifact_exclude_an_untracked_canonical_client_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "working"
    client = source / "clients" / "example"
    client.mkdir(parents=True)
    (client / "package.json").write_text('{"name":"example"}\n')
    (source / "MANIFEST.in").write_text("graft clients\n")
    (source / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "private-source-fixture"
version = "0.1.0"
"""
    )
    _track_source(source)
    secret = client / "untracked-secret.txt"
    secret.write_text("must not ship\n")

    private = copy_private_packaging_source(source, tmp_path / "private")
    build_distribution(private, tmp_path / "dist", artifact="sdist")

    assert not (private.root / secret.relative_to(source)).exists()
    archive_path = next((tmp_path / "dist").glob("private_source_fixture-*.tar.gz"))
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = {member.name.partition("/")[2] for member in archive.getmembers()}
    assert "clients/example/package.json" in members
    assert "clients/example/untracked-secret.txt" not in members


def test_build_rejects_a_forged_private_source_for_the_live_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="created by copy_private_packaging_source"):
        PrivatePackagingSource(_ROOT)

    source = tmp_path / "working"
    source.mkdir()
    _track_source(source)
    issued = copy_private_packaging_source(source, tmp_path / "private")
    forged = copy.copy(issued)
    object.__setattr__(forged, "_PrivatePackagingSource__root", _ROOT)

    with pytest.raises(ValueError, match="created by copy_private_packaging_source"):
        build_distribution(forged, tmp_path / "dist", artifact="wheel")


def test_real_uv_builds_use_the_private_distribution_owner() -> None:
    """A direct build in a test may mutate the checkout beside another reader."""
    offenders = [
        location
        for path in sorted((_ROOT / "tests").rglob("*.py"))
        if path != _OWNER
        for location in _direct_uv_builds(path)
    ]

    assert offenders == [], (
        "real `uv build` calls must use tests.support.distribution_build: " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        'run_test_command(["uv", "build", "--wheel", "--out-dir", str(scratch / "dist")])',
        'run_test_command(["uv", "build", "--wheel", "--out-dir", str(tmp_path)])',
        'run_test_command(["uv", "build", "--sdist", "--out-dir", str(tmp_path)])',
        """\
uv = shutil.which("uv")
assert uv is not None
run_test_command([uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")])
""",
    ],
)
def test_direct_build_guard_recognizes_each_pre_fix_unsafe_command(
    tmp_path: Path, source: str
) -> None:
    fixture = tmp_path / "unsafe_build.py"
    fixture.write_text(source)

    assert len(_direct_uv_builds(fixture)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'run_test_command(("uv", "build", "--wheel", str(output)))',
        'command = ["uv", "build", "--wheel", str(output)]\nrun_test_command(command)',
        """\
uv_path = shutil.which("uv")
command = (uv_path, "build", "--wheel", str(output))
run_test_command(command)
""",
    ],
)
def test_direct_build_guard_recognizes_tuple_and_prebound_commands(
    tmp_path: Path, source: str
) -> None:
    fixture = tmp_path / "unsafe_build.py"
    fixture.write_text(source)

    assert len(_direct_uv_builds(fixture)) == 1


def _direct_uv_builds(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    finder = _DirectUvBuildFinder(path)
    finder.visit(tree)
    return finder.offenders


class _DirectUvBuildFinder(ast.NodeVisitor):
    """Find real uv build prefixes without evaluating dynamic command tails."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._bindings: list[dict[str, ast.expr]] = [{}]
        self.offenders: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node.body)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        self._bind(node.targets, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind((node.target,), node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if node.args and self._command_prefix(node.args[0]) == ("uv", "build"):
            try:
                location = self._path.relative_to(_ROOT)
            except ValueError:
                location = self._path
            self.offenders.append(f"{location}:{node.lineno}")
        self.generic_visit(node)

    def _visit_scope(self, statements: Iterable[ast.stmt]) -> None:
        self._bindings.append({})
        try:
            for statement in statements:
                self.visit(statement)
        finally:
            self._bindings.pop()

    def _bind(self, targets: Iterable[ast.expr], value: ast.expr) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self._bindings[-1][target.id] = value

    def _command_prefix(self, node: ast.expr) -> tuple[str | None, str | None]:
        command = self._resolve(node)
        if not isinstance(command, (ast.List, ast.Tuple)) or len(command.elts) < 2:
            return (None, None)
        return (self._executable(command.elts[0]), self._literal(command.elts[1]))

    def _resolve(self, node: ast.expr, *, seen: frozenset[str] = frozenset()) -> ast.expr:
        if not isinstance(node, ast.Name) or node.id in seen:
            return node
        for scope in reversed(self._bindings):
            if (bound := scope.get(node.id)) is not None:
                return self._resolve(bound, seen=seen | {node.id})
        return node

    def _executable(self, node: ast.expr) -> str | None:
        resolved = self._resolve(node)
        literal = self._literal(resolved)
        if literal is not None:
            return Path(literal).name
        if isinstance(resolved, ast.Name):
            return resolved.id
        if (
            isinstance(resolved, ast.Call)
            and isinstance(resolved.func, (ast.Name, ast.Attribute))
            and (resolved.func.id if isinstance(resolved.func, ast.Name) else resolved.func.attr)
            == "which"
            and resolved.args
        ):
            return self._literal(resolved.args[0])
        return None

    def _literal(self, node: ast.expr) -> str | None:
        resolved = self._resolve(node)
        if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
            return resolved.value
        return None
