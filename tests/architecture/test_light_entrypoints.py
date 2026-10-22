"""The light host commands import only light modules.

``vibesys example``, ``vibesys instances``, ``vibesys tasks``, ``vibesys evals`` and the host
helper are what the desktop app runs on a host, once per action or channel, so
their start-up time is the app's latency. The run engine (``vibesys.api`` and what it loads) takes about
1.7 s to import; these commands must never pay for it. Tach cannot keep them
light on its own: its edges are module granular, and ``server.api`` (run
protocol and engine together) is one module.

This guard computes the *static module-level import closure* of each light
entrypoint: every module its import statements execute when it is loaded,
followed transitively through first-party source and the packages each dotted
name initializes. Imports under ``if TYPE_CHECKING:`` and inside function bodies
are excluded, because they do not run at import;
``tests/e2e/test_light_host_commands.py`` checks what a real process loads.
The closure must stay within ``ALLOWED``; a violation names the import chain
that pulled the module in.
"""

from __future__ import annotations

import ast
import sys
from functools import cache
from pathlib import Path

import pytest
from tests.support.light_imports import is_heavy
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (REPO_ROOT / "src", *sorted((REPO_ROOT / "libs").glob("*/src")))

LIGHT_ENTRYPOINTS = (
    "entrypoints.launcher",
    "entrypoints.examples",
    "entrypoints.instances",
    "entrypoints.tasks",
    "entrypoints.evals",
    "entrypoints.detached_run",
    "entrypoints.host_helper",
)
"""Modules a light host command loads first. The launcher routes on argv before
it loads anything heavy, so its own module level is held to the same bound."""

ALLOWED_THIRD_PARTY = frozenset({"pydantic", "pydantic_core", "typing_extensions"})
"""Third-party top-level packages the light closure may import (not traversed)."""

ALLOWED_FIRST_PARTY = (
    "entrypoints",
    "entrypoints.launcher",
    "entrypoints.examples",
    "entrypoints.example_catalog",
    "entrypoints.instances",
    "entrypoints.tasks",
    "entrypoints.evals",
    "entrypoints.detached_run",
    "entrypoints.host_helper",
    "server",
    "server.control_envelope",
    "server.diagnostics",
    "server.host_api",
    "server.host_helper",
    "server.host_helper_logs",
    "server.host_helper_machine",
    "server.host_helper_compute",
    "server.host_compute",
    "server.host_agent_logins",
    "server.host_examples",
    "server.host_harness",
    "server.host_helper_preflight",
    "server.host_machine_facts",
    "server.host_preflight",
    "server.host_helper_os",
    "server.host_helper_requests",
    "server.host_run_events",
    "server.host_run_reader",
    "server.host_helper_readers",
    "server.host_envelope",
    "server.host_operation_types_generated",
    "server.host_run_dashboard",
    "server.host_run_summary",
    "server.run_summary",
    "server.run_summary_cache",
    "server.instances",
    "server.registry",
    "server.relay",
    "server.run_digest",
    "server.span_index",
    "vs_sim",
    "vs_project",
    "vs_decode",
    "vs_redaction",
    "vs_logs",
    "vs_slurm",
)
"""First-party modules, or packages with everything under them, the closure may hold.

``vs_sim``, ``vs_project``, ``vs_decode``, ``vs_redaction``, ``vs_logs`` and ``vs_slurm``
are allowed whole: they are the light libraries the host commands are built on
(``vs_redaction`` imports only the standard library; ``vs_decode`` imports only pydantic;
``vs_logs``, which a detached run needs to buffer its narration before the engine loads and
the host helper reads run logs with, only pydantic, ``vs_decode`` and ``vs_sim``; ``vs_slurm``
only pydantic, ``vs_decode`` and ``vs_sim``), and their own boundaries are tach's job."""


def _module_file(name: str, roots: tuple[Path, ...] = SOURCE_ROOTS) -> Path | None:
    """Return the first-party source file for ``name``, or None if it is not first-party."""
    parts = name.split(".")
    for root in roots:
        package = root.joinpath(*parts) / "__init__.py"
        if package.is_file():
            return package
        module = root.joinpath(*parts[:-1]) / f"{parts[-1]}.py"
        if module.is_file():
            return module
    return None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _executed_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Statements that run when the module is imported (functions' bodies do not)."""
    executed: list[ast.stmt] = []
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        executed.append(statement)
        if isinstance(statement, ast.If):
            if not _is_type_checking(statement.test):
                executed.extend(_executed_statements(statement.body))
            executed.extend(_executed_statements(statement.orelse))
        elif isinstance(statement, ast.Try):
            for block in (statement.body, statement.orelse, statement.finalbody):
                executed.extend(_executed_statements(block))
            for handler in statement.handlers:
                executed.extend(_executed_statements(handler.body))
        elif isinstance(statement, (ast.With, ast.ClassDef)):
            executed.extend(_executed_statements(statement.body))
    return executed


def _with_parents(name: str) -> list[str]:
    """``a.b.c`` initializes ``a`` and ``a.b`` before ``a.b.c``."""
    parts = name.split(".")
    return [".".join(parts[: index + 1]) for index in range(len(parts))]


@cache
def direct_imports(name: str, roots: tuple[Path, ...] = SOURCE_ROOTS) -> tuple[str, ...]:
    """Modules ``name``'s module level imports, with the packages each initializes."""
    path = _module_file(name, roots)
    if path is None:
        return ()
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: list[str] = []
    for statement in _executed_statements(parse_source(path.read_text(encoding="utf-8")).body):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                found.extend(_with_parents(alias.name))
        elif isinstance(statement, ast.ImportFrom):
            base = statement.module or ""
            if statement.level:
                anchor = package.split(".")[: len(package.split(".")) - statement.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            found.extend(_with_parents(base))
            # `from package import submodule` loads the submodule.
            found.extend(
                f"{base}.{alias.name}"
                for alias in statement.names
                if _module_file(f"{base}.{alias.name}", roots) is not None
            )
    return tuple(dict.fromkeys(found))


def import_closure(root: str, roots: tuple[Path, ...] = SOURCE_ROOTS) -> dict[str, tuple[str, ...]]:
    """Every module ``root`` loads at import, mapped to the chain that first reached it."""
    chains: dict[str, tuple[str, ...]] = {root: (root,)}
    pending = [root, *_with_parents(root)[:-1]]
    for parent in pending[1:]:
        chains.setdefault(parent, (root, parent))
    while pending:
        current = pending.pop()
        for imported in direct_imports(current, roots):
            if imported in chains:
                continue
            chains[imported] = (*chains[current], imported)
            if _module_file(imported, roots) is not None:
                pending.append(imported)
    return chains


def _within(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _allowed(name: str, roots: tuple[Path, ...] = SOURCE_ROOTS) -> bool:
    if _module_file(name, roots) is not None:
        exact = name in ALLOWED_FIRST_PARTY
        whole_package = _within(
            name,
            ("vs_sim", "vs_project", "vs_decode", "vs_redaction", "vs_logs", "vs_slurm"),
        )
        return (exact or whole_package) and not is_heavy(name)
    top = name.partition(".")[0]
    return (top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY) and not is_heavy(name)


def violations(root: str, roots: tuple[Path, ...] = SOURCE_ROOTS) -> list[str]:
    """Each module outside the allowlist, with the import chain that loaded it."""
    return [
        f"{name}: {' -> '.join(chain)}"
        for name, chain in sorted(import_closure(root, roots).items())
        if not _allowed(name, roots)
    ]


@pytest.mark.parametrize("entrypoint", LIGHT_ENTRYPOINTS)
def test_light_entrypoint_imports_only_light_modules(entrypoint: str) -> None:
    assert _module_file(entrypoint) is not None, f"{entrypoint} has moved; update this guard"
    assert violations(entrypoint) == []


# --- the guard's own behavior ----------------------------------------------------


def test_the_closure_follows_packages_and_submodules_but_not_deferred_imports() -> None:
    closure = import_closure("entrypoints.instances")

    assert closure["server.registry"] == ("entrypoints.instances", "server.registry")
    assert "server" in closure
    # `instances stop` reaches a server through the control envelope alone.
    assert closure["server.control_envelope"] == (
        "entrypoints.instances",
        "server.instances",
        "server.control_envelope",
    )


def test_a_heavy_module_is_reported_with_the_chain_that_loaded_it(tmp_path: Path) -> None:
    # A protocol module that builds on the control envelope and loads the engine: a small
    # tree with the same shape, so the closure walk does not cross the whole product.
    package = tmp_path / "server"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "protocol.py").write_text("from server import api\nfrom server import envelope\n")
    (package / "envelope.py").write_text("import json\n")
    (package / "api.py").write_text("import vibesys.api\n")

    found = violations("server.protocol", (tmp_path,))

    assert any(line.startswith("server.api: server.protocol -> server.api") for line in found)
    assert any(line.startswith("vibesys") for line in found)


def test_type_checking_blocks_and_function_bodies_are_not_executed() -> None:
    source = """
import json
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import heavy_typing_only
else:
    import light_runtime
def deferred():
    import heavy_deferred
class Model:
    import in_class_body
try:
    import optional
except ImportError:
    import fallback
"""
    executed = _executed_statements(parse_source(source).body)
    names = {
        alias.name
        for statement in executed
        if isinstance(statement, ast.Import)
        for alias in statement.names
    }

    assert names == {"json", "light_runtime", "in_class_body", "optional", "fallback"}
