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
import importlib
import inspect
import pkgutil
from pathlib import Path

from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[2]
_TOML_READERS = frozenset({"load", "loads"})

# Built by the loader from resolved paths; never parsed from a file.
_RESOLVED_RESULT_MODELS = frozenset({"InputBundle"})

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
    ("src/entrypoints/cli/config.py", "_restore_project_config"): (
        "probes the raw file for explicitly set keys; load_config_and_skills validates it"
    ),
    ("src/vibesys/orchestration/skill_selection.py", "load_sidecar_rules"): (
        "hand parser that rejects unknown top-level and rule keys by name"
    ),
}


def _source_files() -> list[Path]:
    return sorted([*(_ROOT / "src").rglob("*.py"), *_ROOT.glob("libs/*/src/**/*.py")])


def _module_name(path: Path) -> str:
    relative = path.relative_to(_ROOT)
    parts = relative.with_suffix("").parts
    return ".".join(parts[1:] if parts[0] == "src" else parts[3:])


def _toml_reader_names(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return (names bound to the tomllib module, names bound to its readers)."""
    modules: set[str] = set()
    readers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "tomllib"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "tomllib":
            readers.update(
                alias.asname or alias.name for alias in node.names if alias.name in _TOML_READERS
            )
    return frozenset(modules), frozenset(readers)


def _is_toml_read(node: ast.AST, modules: frozenset[str], readers: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in readers
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _TOML_READERS
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    )


def _validated_models(function: ast.AST) -> list[ast.expr]:
    """Return the receivers of every ``model_validate`` call in *function*."""
    return [
        node.func.value
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_validate"
    ]


def _toml_readers() -> dict[tuple[str, str], list[ast.expr]]:
    """Map each function that reads TOML to the models it validates against."""
    found: dict[tuple[str, str], list[ast.expr]] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, readers = _toml_reader_names(tree)
        relative = path.relative_to(_ROOT).as_posix()
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(_is_toml_read(node, modules, readers) for node in ast.walk(function)):
                found[(relative, function.name)] = _validated_models(function)
    return found


def _unvalidated_toml_readers() -> set[tuple[str, str]]:
    return {key for key, models in _toml_readers().items() if not models}


def test_toml_is_read_through_a_validating_model() -> None:
    unexpected = _unvalidated_toml_readers() - _ALLOWED.keys()

    assert not unexpected, (
        "These functions read TOML without model_validate; parse the file through a "
        "Pydantic model with extra='forbid' (see vibesys.inputs.load_objectives): "
        f"{sorted(unexpected)}"
    )


def test_every_validating_model_forbids_unknown_keys() -> None:
    """A model that ignores extras defeats the point of validating at all."""
    lax: list[str] = []
    for (relative, function), models in _toml_readers().items():
        if (relative, function) in _ALLOWED:
            continue
        module = importlib.import_module(_module_name(_ROOT / relative))
        for receiver in models:
            model = getattr(module, receiver.id, None) if isinstance(receiver, ast.Name) else None
            if model is None or model.model_config.get("extra") != "forbid":
                lax.append(f"{relative}:{function} validates {ast.unparse(receiver)}")

    assert not lax, f"TOML models must be BaseModels with extra='forbid': {lax}"


def test_every_input_bundle_model_forbids_unknown_keys() -> None:
    """Models in ``vibesys.inputs`` describe authored files; none may ignore extras."""
    package = importlib.import_module("vibesys.inputs")
    lax = []
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"vibesys.inputs.{info.name}")
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
                and value.model_config.get("extra") != "forbid"
                and name not in _RESOLVED_RESULT_MODELS
            ):
                lax.append(f"{module.__name__}.{name}")

    assert not lax, f"Authored-input models must set extra='forbid': {lax}"


def test_allowlist_has_no_stale_entries() -> None:
    stale = _ALLOWED.keys() - _toml_readers().keys()

    assert not stale, f"Remove allowlist entries that no longer read TOML: {sorted(stale)}"
