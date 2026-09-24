"""Guard: profiler tool code and prompts must not hardcode a serving engine.

MCP tooling and profiler prompts are meant to work against any candidate
process, not just vLLM or SGLang: engine-specific knowledge (multiprocessing
model, torch.profiler contract, graceful-shutdown behavior, ...) belongs in
the serving-systems skill tree
(``resources/skills/serving-systems/references/engines/`` and
``references/tooling/profiling-serving-engines.md``), not in tool code or
agent prompts. A previous pass removed every such mention from the profiler
prompt templates and the ``resources/profilers`` analyzer CLIs; this test
keeps that true going forward.

Scope is deliberately narrow: only the profiler prompt templates
(``prompts/loops/agent/profilers/*.j2`` and every backend's
``profiling_workflow.j2`` fragment) and ``resources/profilers/**/*.py``.
Other prompts (e.g. the implementer prompt) legitimately discuss vLLM/SGLang
as candidate source trees the agent may be optimizing and are out of scope
here.
"""

from __future__ import annotations

import re
from pathlib import Path

from vibesys.constants import PROJECT_ROOT

_ENGINE_NAME_RE = re.compile(r"vllm|sglang", re.IGNORECASE)

_PROMPT_GLOBS = (
    "src/vibesys/prompts/loops/agent/profilers/*.j2",
    "src/vibesys/prompts/backend/*/profiling_workflow.j2",
)
_PROFILER_CODE_ROOT = "resources/profilers"


def _matches(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _ENGINE_NAME_RE.search(line):
            hits.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_profiler_prompt_templates_name_no_serving_engine() -> None:
    paths: list[Path] = []
    for pattern in _PROMPT_GLOBS:
        paths.extend(sorted(PROJECT_ROOT.glob(pattern)))
    assert paths, f"no profiler prompt templates found under {_PROMPT_GLOBS}"

    failures = [hit for path in paths for hit in _matches(path)]
    assert not failures, "engine name found in a profiler prompt template:\n" + "\n".join(
        failures
    )


def test_profiler_resource_code_names_no_serving_engine() -> None:
    root = PROJECT_ROOT / _PROFILER_CODE_ROOT
    paths = sorted(
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and p.is_file()
    )
    assert paths, f"no profiler resource code found under {root}"

    failures = [hit for path in paths for hit in _matches(path)]
    assert not failures, "engine name found in resources/profilers code:\n" + "\n".join(
        failures
    )
