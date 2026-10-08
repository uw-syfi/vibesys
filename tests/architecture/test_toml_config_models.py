"""Architecture contract: authored TOML is parsed through a validating model.

Every config surface in this repository declares a Pydantic model with
``extra="forbid"`` and reads the file with ``Model.model_validate``. A function
that instead reads TOML into a dict and picks keys with ``.get`` silently
ignores a misspelled key and substitutes the default, which for a tolerance or
an axis list is a wrong answer with no error.

The scan is syntactic: outside the allowlist, a function that calls
``tomllib.load`` or ``tomllib.loads`` must call ``model_validate`` in the same
function. The allowlist names the sites that read a file VibeSys does not own
the schema of, or whose hand parser rejects unknown keys itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TOML_READERS = frozenset({"load", "loads"})

_ALLOWED: dict[tuple[str, str], str] = {
    ("src/entrypoints/launcher.py", "source_checkout_root"): (
        "identifies the checkout by pyproject name; not a VibeSys config surface"
    ),
    ("libs/vs-runtime/src/vs_runtime/_input_project.py", "_path_sources"): (
        "reads a third-party pyproject.toml (uv sources)"
    ),
    ("libs/vs-runtime/src/vs_runtime/_input_project.py", "_path_sources_from_text"): (
        "reads a third-party pyproject.toml (uv sources)"
    ),
    ("libs/vs-runtime/src/vs_runtime/_input_project.py", "_project_name"): (
        "reads a third-party pyproject.toml (project name)"
    ),
    ("src/vibesys/orchestration/skill_selection.py", "load_sidecar_rules"): (
        "hand parser that rejects unknown top-level and rule keys by name"
    ),
}


def _source_files() -> list[Path]:
    return sorted([*(_ROOT / "src").rglob("*.py"), *_ROOT.glob("libs/*/src/**/*.py")])


def _is_toml_read(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _TOML_READERS
        and isinstance(func.value, ast.Name)
        and func.value.id == "tomllib"
    )


def _calls_model_validate(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_validate"
        for node in ast.walk(function)
    )


def _unvalidated_toml_readers() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(_ROOT).as_posix()
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            reads_toml = any(_is_toml_read(node) for node in ast.walk(function))
            if reads_toml and not _calls_model_validate(function):
                found.add((relative, function.name))
    return found


def test_toml_is_read_through_a_validating_model() -> None:
    unexpected = _unvalidated_toml_readers() - _ALLOWED.keys()

    assert not unexpected, (
        "These functions read TOML without model_validate; parse the file through a "
        "Pydantic model with extra='forbid' (see vibesys.inputs.load_objectives): "
        f"{sorted(unexpected)}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = _ALLOWED.keys() - _unvalidated_toml_readers()

    assert not stale, f"Remove allowlist entries that read validated TOML now: {sorted(stale)}"
