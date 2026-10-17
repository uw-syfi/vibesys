"""Each scripted edit of the queue example does to the real queue what its kind says.

The scripted implementer's recipes (``vibesys.orchestration.dynamic.scripted``) are edits of
``src/lib.rs`` in the Rust queue example. A recipe that claims to improve, to change nothing or to
slow the queue down must still build and pass the trusted check; one that claims to break the build
must not build; one that claims to break accuracy must build and fail the check. Each is built with
``make`` and checked by the Go queue evaluator, as a run does, so this needs ``cargo`` and ``go``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vibesys.orchestration.dynamic.scripted.api import QUEUE_RECIPES, EditKind, Recipe

if TYPE_CHECKING:
    import subprocess

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE_EXAMPLE = REPO_ROOT / "examples/data-structures/repositories/queue-rs"
EVALUATOR = REPO_ROOT / "resources/evaluators/queue"
BUILD_TIMEOUT_S = 600
CHECK_TIMEOUT_S = 300

pytestmark = pytest.mark.skipif(
    shutil.which("cargo") is None or shutil.which("go") is None,
    reason="the queue example needs the Rust and Go toolchains",
)


def _build(workspace: Path) -> subprocess.CompletedProcess[str]:
    return run_test_command(
        ["make", "queue-candidate.so"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_S,
    )


def _check(workspace: Path) -> subprocess.CompletedProcess[str]:
    command = ["go", "-C", str(EVALUATOR), "run", ".", "check", "--workspace", str(workspace)]
    command += ["--scenario", "mpmc", "--operations", "24", "--trials", "30"]
    return run_test_command(command, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S)


@pytest.mark.parametrize("recipe", QUEUE_RECIPES, ids=lambda recipe: recipe.slug)
def test_a_recipe_builds_and_passes_the_check_exactly_when_its_kind_says(
    recipe: Recipe, tmp_path: Path
) -> None:
    workspace = tmp_path / "queue"
    shutil.copytree(QUEUE_EXAMPLE, workspace, ignore=shutil.ignore_patterns("target"))
    source = workspace / "src/lib.rs"
    edited = recipe.apply(source.read_text(encoding="utf-8"))
    assert edited is not None
    source.write_text(edited, encoding="utf-8")

    build = _build(workspace)

    if recipe.kind is EditKind.BREAKS_BUILD:
        assert build.returncode != 0, "the edit was meant not to compile"
        return
    assert build.returncode == 0, build.stderr[-2000:]
    check = _check(workspace)
    if recipe.kind is EditKind.BREAKS_ACCURACY:
        assert check.returncode != 0, "the edit was meant to fail the trusted check"
    else:
        assert check.returncode == 0, check.stdout[-2000:] + check.stderr[-2000:]
