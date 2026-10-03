"""Pure orchestration policy stays separate from effectful product adapters."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "vibesys"
_RUN = _SRC / "run"

_EVALUATION_ADAPTERS = {
    "create_evaluation",
    "trusted_evaluation_plan",
}
_SKILL_POLICY = {
    "platform_skill_excluded_paths",
    "platform_skill_selection",
    "resolve_skill_source_paths",
}
_SKILL_ADAPTERS = {"resolve_skill_source_dirs", "validate_skill_tree"}
_PROFILER_ADAPTERS = {"native_profiler_preflight", "resolve_run_profiler"}


def _top_level_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    }


def test_effectful_adapters_are_owned_by_run_modules() -> None:
    skill_selection = _SRC / "orchestration" / "skill_selection.py"
    profilers = _SRC / "orchestration" / "profilers.py"

    assert not (_SRC / "orchestration" / "evaluation.py").exists()
    assert _top_level_definitions(_RUN / "evaluation.py") >= _EVALUATION_ADAPTERS
    assert _top_level_definitions(skill_selection) >= _SKILL_POLICY
    assert not (_top_level_definitions(skill_selection) & _SKILL_ADAPTERS)
    assert _top_level_definitions(_RUN / "skill_sources.py") >= _SKILL_ADAPTERS
    assert not (_top_level_definitions(profilers) & _PROFILER_ADAPTERS)
    assert _top_level_definitions(_RUN / "profilers.py") >= _PROFILER_ADAPTERS
