"""Shared orchestration policy stays out of runtime-composition modules."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_RUN = _SRC / "run"

_EVALUATION_POLICY = {
    "create_evaluation",
    "trusted_evaluation_plan",
}
_SKILL_SELECTION_POLICY = {
    "platform_skill_excluded_paths",
    "platform_skill_selection",
    "resolve_skill_source_dirs",
    "resolve_skill_source_paths",
}


def _top_level_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    }


def test_shared_policy_is_owned_by_orchestration_modules() -> None:
    evaluation = _SRC / "orchestration" / "evaluation.py"
    skill_selection = _SRC / "orchestration" / "skill_selection.py"

    assert not (_RUN / "evaluation.py").exists()
    assert not (_RUN / "skills.py").exists()
    assert _top_level_definitions(evaluation) >= _EVALUATION_POLICY
    assert _top_level_definitions(skill_selection) >= _SKILL_SELECTION_POLICY

    forbidden = _EVALUATION_POLICY | _SKILL_SELECTION_POLICY
    violations = {
        str(path.relative_to(_SRC)): sorted(_top_level_definitions(path) & forbidden)
        for path in _RUN.rglob("*.py")
        if _top_level_definitions(path) & forbidden
    }
    assert not violations, f"shared orchestration policy returned under vibesys.run: {violations}"
