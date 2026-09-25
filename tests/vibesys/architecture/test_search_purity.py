"""``vibesys.search`` stays pure: deterministic state transitions, no effects.

Rules (see the orchestration-simplify design brief):
  - No imports of vibesys.orchestration, vibesys.loops, vibesys.roles,
    vibesys.prompts (search answers questions and returns new state; it never
    drives agents, renders prompts, or touches RunContext).
  - No os / subprocess / pathlib / time / datetime imports (search must do no
    I/O and touch no clock; every effect is deterministic and resume-safe).
  - ``random`` may be imported freely (for the ``Random`` type and
    ``getstate``/``setstate`` state plumbing), but the module-level RNG
    functions (``random.random()``, ``random.choice()``, ...) may never be
    called -- they read/mutate process-global state that resume can't see --
    and every bare ``random.Random()`` construction must immediately restore
    its state from the persisted state value, so RNG state always lives in
    the state value, never in an unseeded instance or the global singleton.

All three checks are pure ``ast`` scans, so they do not require importing
``vibesys.search`` (which would pull in its third-party dependencies).
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_SEARCH = _SRC / "search"

_FORBIDDEN_PACKAGES = ("vibesys.orchestration", "vibesys.loops", "vibesys.roles", "vibesys.prompts")

# vibesys.agent_run has fully dissolved into search/hypothesis, roles/,
# loops/, and vibesys.orchestration.{memory,artifacts}. No search/ module
# re-exports from it any more.
_ALLOWED_AGENT_RUN_REEXPORTS: set[str] = set()

_FORBIDDEN_CLOCK_OR_IO_MODULES = ("os", "subprocess", "pathlib", "time", "datetime")

# Process-global RNG accessors upstream OpenEvolve code itself reads/writes.
# ``_upstream_random`` (search/population/openevolve_selector.py) is the one
# place search/ may call these directly: it swaps the global singleton's
# state for an explicit, state-derived ``random.Random`` around a call into
# upstream code that only knows the global RNG, then restores the process's
# own state in a ``finally``. ``getrandbits`` is included because that same
# function also stands in for upstream's ``uuid.uuid4()`` (OS entropy, not
# seeded) so every id upstream mints is a deterministic function of the same
# swapped-in state. Every other module-level ``random.<name>()`` call reads
# or mutates state resume can't see, so it stays forbidden.
_ALLOWED_GLOBAL_RANDOM_CALLS = {"getstate", "setstate", "getrandbits"}


def _module_level_import_nodes(path: Path) -> list[tuple[ast.stmt, str]]:
    """Return (node, module_name) for every top-level (non-TYPE_CHECKING) import."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[tuple[ast.stmt, str]] = []
    for node in tree.body:
        if _is_type_checking_guard(node):
            continue
        if isinstance(node, ast.Import):
            out.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node, node.module))
    return out


def _is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return False


def test_search_imports_nothing_from_orchestration_loops_roles_or_prompts() -> None:
    violations: list[str] = []
    for path in _SEARCH.rglob("*.py"):
        for node, module_name in _module_level_import_nodes(path):
            if any(
                module_name == pkg or module_name.startswith(pkg + ".")
                for pkg in _FORBIDDEN_PACKAGES
            ):
                violations.append(f"{path.relative_to(_SRC)}:{node.lineno} imports {module_name}")
    assert not violations, "search/ imports a forbidden layer: " + "; ".join(violations)


def test_search_has_no_module_level_io_or_clock_imports() -> None:
    violations: list[str] = []
    for path in _SEARCH.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        for node, module_name in _module_level_import_nodes(path):
            if module_name.startswith("vibesys.agent_run"):
                if rel not in _ALLOWED_AGENT_RUN_REEXPORTS:
                    violations.append(
                        f"{rel}:{node.lineno} imports {module_name} (not an allowlisted re-export)"
                    )
                continue
            top = module_name.split(".", 1)[0]
            if top in _FORBIDDEN_CLOCK_OR_IO_MODULES:
                violations.append(f"{rel}:{node.lineno} imports {module_name}")
    assert not violations, "search/ has an I/O or clock import: " + "; ".join(violations)


def _enclosing_statement_list(tree: ast.AST, target: ast.stmt) -> list[ast.stmt] | None:
    """Return the statement list that directly contains ``target``, if any."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list) and target in body:
                return body
    return None


def _is_bare_random_dot(call: ast.Call, attr: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == attr
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "random"
    )


def _restores_from_state(stmt: ast.stmt, target: str) -> bool:
    """Whether ``stmt`` is ``<target>.setstate(<expr containing "state">)``."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "setstate"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == target
        and call.args
    ):
        return False
    return "state" in ast.unparse(call.args[0]).lower()


def test_search_uses_no_global_rng_and_seeds_every_random_instance() -> None:
    violations: list[str] = []
    for path in _SEARCH.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_bare_random_dot(node, "Random"):
                if node.args or node.keywords:
                    continue  # seeded directly, e.g. random.Random(config.seed)
                assign = next(
                    (
                        candidate
                        for candidate in ast.walk(tree)
                        if isinstance(candidate, ast.Assign)
                        and candidate.value is node
                        and len(candidate.targets) == 1
                        and isinstance(candidate.targets[0], ast.Name)
                    ),
                    None,
                )
                restored = False
                if assign is not None:
                    body = _enclosing_statement_list(tree, assign)
                    target_node = assign.targets[0]
                    if body is not None and isinstance(target_node, ast.Name):
                        tail = body[body.index(assign) + 1 :]
                        restored = any(
                            _restores_from_state(later, target_node.id) for later in tail
                        )
                if not restored:
                    violations.append(
                        f"{rel}:{node.lineno} random.Random() is never seeded and its "
                        "state is not immediately restored from a state value"
                    )
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "random"
                and node.func.attr not in _ALLOWED_GLOBAL_RANDOM_CALLS
                and node.func.attr != "Random"
            ):
                violations.append(
                    f"{rel}:{node.lineno} calls the module-global random.{node.func.attr}(...)"
                )
    assert not violations, "search/ RNG use is not state-derived: " + "; ".join(violations)
