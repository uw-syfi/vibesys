"""Which modules are flow code: the one definition the flow checks share.

A flow is `async def` code written against `vs_core.api.search.Flow`. Flows are async by design;
what keeps them deterministic is `tests/architecture/test_flow_code_is_restricted.py` (no clock,
randomness, I/O or `asyncio` imports) plus the runtime's rejection of foreign awaitables. The
purity ratchet (`scripts/check_purity.py`) therefore does not count `async`/`await` in these
modules, and still flags every other kind of impurity there.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from scripts.repo_files import tracked_files

if TYPE_CHECKING:
    from pathlib import Path

SEARCH_API = "vs_core.api.search"

#: Packages whose every module is flow code, relative to the repository root.
FLOW_GLOBS = ("src/vibesys/orchestration/*/search/**/*.py", "src/vibesys/candidates/flows.py")

#: The flow framework: the runtime and test kit that implement `ctx`. They are async because the
#: flows they run are, but they own the effects, so the architecture test's import ban does not
#: apply to them.
FRAMEWORK_GLOBS = (
    "libs/vs-core/src/vs_core/flows/**/*.py",
    "libs/vs-core/src/vs_core/testing/flows.py",
)


def imports_flow(tree: ast.Module) -> bool:
    """Whether the module imports `Flow` from the search API, which makes it flow code."""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == SEARCH_API
        and any(alias.name == "Flow" for alias in node.names)
        for node in ast.walk(tree)
    )


def flow_modules(root: Path) -> list[Path]:
    """Modules under `src/` that are flow code: the flow packages plus any importer of `Flow`."""
    found = {path for pattern in FLOW_GLOBS for path in root.glob(pattern)}
    for path in tracked_files(root, suffixes=(".py",), under=("src",)):
        if imports_flow(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            found.add(path)
    return sorted(found)


def async_allowed_paths(root: Path) -> frozenset[str]:
    """Repository-relative paths where `async`/`await` is the point: flow code and its framework."""
    paths = [*flow_modules(root), *(p for pattern in FRAMEWORK_GLOBS for p in root.glob(pattern))]
    return frozenset(path.relative_to(root).as_posix() for path in paths)
