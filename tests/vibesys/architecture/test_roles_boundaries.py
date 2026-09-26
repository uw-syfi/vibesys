"""``vibesys.roles`` is data only, and its catalog carries no dead roles.

- roles/ declares ``Role`` instances, prompt context models, and reply
  schemas. It never imports vibesys.orchestration or vibesys.loops: turn
  execution, sequencing, and state transitions stay in those layers.
- The catalog is complete: every ``Role`` any strategy uses lives in
  roles/, and every ``Role`` declared in roles/ is used by at least one
  registered strategy under vibesys.loops (no dead roles). This mirrors
  ``tests/vibesys/roles/test_catalog.py`` but as a standalone architecture
  check, driven by a plain static name search rather than that test's
  fuller template/id validation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_ROLES = _SRC / "roles"
_LOOPS = _SRC / "loops"

_FORBIDDEN_PACKAGES = ("vibesys.orchestration", "vibesys.loops")

# Roles declared but not yet referenced from any strategy's loops/ folder.
# Keep empty over time; a Role that stays unused should be deleted.
_ALLOWED_UNUSED_ROLE_NAMES: set[str] = set()

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _module_level_import_nodes(path: Path) -> list[tuple[ast.stmt, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node, node.module))
    return out


def test_roles_does_not_import_orchestration_or_loops() -> None:
    violations: list[str] = []
    for path in _ROLES.rglob("*.py"):
        for node, module_name in _module_level_import_nodes(path):
            if any(
                module_name == pkg or module_name.startswith(pkg + ".")
                for pkg in _FORBIDDEN_PACKAGES
            ):
                violations.append(f"{path.relative_to(_SRC)}:{node.lineno} imports {module_name}")
    assert not violations, "roles/ imports orchestration or loops: " + "; ".join(violations)


def _binds_a_role(value: ast.expr) -> bool:
    """True for `Role(...)` or a dict family `{..., key: Role(...), ...}`."""
    if isinstance(value, ast.Call):
        return _is_role_call(value)
    if isinstance(value, ast.Dict):
        return any(isinstance(v, ast.Call) and _is_role_call(v) for v in value.values if v)
    return False


def _role_names() -> set[str]:
    """Every module-level identifier bound to a ``Role(`` call in roles/."""
    names: set[str] = set()
    for path in _ROLES.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not _binds_a_role(node.value):
                continue
            names.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name) and _IDENTIFIER.match(target.id)
            )
    return names


def _is_role_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "Role"
    if isinstance(func, ast.Attribute):
        return func.attr == "Role"
    return False


@pytest.mark.skip(
    reason=(
        "TODO(stack PR 09): unskip. Strategies have not yet been migrated to "
        "call through vibesys.roles; they land in stack PRs 07-09. Until then "
        "no Role is reachable from loops/, so this always fails."
    )
)
def test_every_declared_role_is_referenced_by_a_loops_module() -> None:
    role_names = _role_names() - _ALLOWED_UNUSED_ROLE_NAMES
    loops_source = "\n".join(path.read_text() for path in _LOOPS.rglob("*.py"))
    unused = sorted(
        name for name in role_names if not re.search(rf"\b{re.escape(name)}\b", loops_source)
    )
    assert not unused, f"roles declared but never referenced from loops/: {unused}"
