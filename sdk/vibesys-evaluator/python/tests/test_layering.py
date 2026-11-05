"""The package's import graph: the core knows no domain, and domains import only the core.

Every module under ``src/vibesys_evaluator`` is read (imports under
``TYPE_CHECKING`` and relative imports included). A module belongs to the
domain whose package it is in (``microservices``, ``llm_serving``) or to the
core. The core imports no domain; a domain imports the core and itself only.
This is the package's own check (``docs/spec.md``, "Layout"); the repository's
tach graph does not cover ``sdk/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = "vibesys_evaluator"
DOMAINS = ("microservices", "llm_serving")


def _module(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(source: str, module: str, *, is_package: bool) -> list[str]:
    """The absolute names of the modules ``module`` imports from this package."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                base = base if is_package else base[:-1]
                base = base[: len(base) - (node.level - 1)]
                target = ".".join([*base, node.module] if node.module else base)
            else:
                target = node.module or ""
            found.append(target)
            found.extend(f"{target}.{alias.name}" for alias in node.names)
    return [name for name in found if name == PACKAGE or name.startswith(PACKAGE + ".")]


def owner(module: str) -> str:
    """``core`` or the domain a module belongs to."""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[1] in DOMAINS else "core"


def violations(module: str, imported: list[str]) -> list[str]:
    """Each import of ``module`` that breaks the layering, as ``module -> imported``."""
    mine = owner(module)
    bad = []
    for name in imported:
        theirs = owner(name)
        if theirs not in {"core", mine}:
            bad.append(f"{module} ({mine}) -> {name} ({theirs})")
    return bad


def test_no_core_module_imports_a_domain_and_no_domain_imports_another() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 20, "no package source found; the layout changed"
    problems = []
    for path in files:
        module = _module(path)
        imported = _imports(path.read_text(), module, is_package=path.name == "__init__.py")
        problems += violations(module, imported)
    assert not problems, "\n".join(problems)


def test_every_domain_is_a_package_of_its_own() -> None:
    for domain in DOMAINS:
        assert (SRC / PACKAGE / domain / "__init__.py").is_file(), domain


@pytest.mark.parametrize(
    ("module", "source", "is_package", "bad"),
    [
        pytest.param(
            "vibesys_evaluator._private.run",
            "from vibesys_evaluator.microservices import Run",
            False,
            True,
            id="core-to-domain",
        ),
        pytest.param(
            "vibesys_evaluator.llm_serving.golden",
            "from ..microservices._private import calls",
            False,
            True,
            id="relative-cross-domain",
        ),
        pytest.param(
            "vibesys_evaluator.microservices",
            "if TYPE_CHECKING:\n    import vibesys_evaluator.llm_serving.agentx",
            True,
            True,
            id="type-checking-cross-domain",
        ),
        pytest.param(
            "vibesys_evaluator.llm_serving.agentx",
            "from vibesys_evaluator._private.harness import run_harness\nfrom . import _agentx",
            False,
            False,
            id="domain-to-core-and-itself",
        ),
        pytest.param(
            "vibesys_evaluator",
            "from vibesys_evaluator import llm_serving",
            True,
            True,
            id="core-imports-domain-by-name",
        ),
    ],
)
def test_the_check_tells_a_violation_from_an_allowed_import(
    module: str, source: str, *, is_package: bool, bad: bool
) -> None:
    assert bool(violations(module, _imports(source, module, is_package=is_package))) is bad
