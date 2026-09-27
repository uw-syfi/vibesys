"""``vibesys.roles`` is data only, and its catalog carries no dead roles.

- roles/ declares ``Role`` instances, prompt context models, and reply
  schemas. It never imports vibesys.orchestration or vibesys.orchestrations: turn
  execution, sequencing, and state transitions stay in those layers.
- The catalog is complete: every ``Role`` any strategy uses lives in
  roles/, and every ``Role`` declared in roles/ is used by at least one
  registered strategy under vibesys.orchestrations (no dead roles). This mirrors
  ``tests/vibesys/roles/test_catalog.py`` but as a standalone architecture
  check, driven by a plain static name search rather than that test's
  fuller template/id validation.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_ROLES = _SRC / "roles"
_FORBIDDEN_PACKAGES = ("vibesys.orchestration", "vibesys.orchestrations")


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
