"""Local validation runs agent-authored recipes in a throwaway workspace at a retained revision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_core.api import RevisionId, RevisionRef, Until, ValidateLocalBase
from vs_runtime.api import CommandResult, member_workspace_id
from vs_runtime.api.core import ExecutionContext, NotApplied, ValidateLocalOwner
from vs_runtime.api.infrastructure import LocalValidator
from vs_runtime.api.testing import (
    FakeCommands,
    FakeWorkspace,
    FakeWorkspaces,
    MemoryValidationPasses,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import ArtifactStore
    from vs_runtime.api.infrastructure import FrameworkValidationResult, ValidationRecipe


class Validate(ValidateLocalBase):
    kind: Literal["demo.validate_local"] = "demo.validate_local"


@dataclass(frozen=True)
class _Receipt:
    sha256: str


class _Store:
    """The artifact store's write side, in memory and content addressed."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, content: bytes) -> _Receipt:
        self.written.append(content)
        return _Receipt(hashlib.sha256(content).hexdigest())


def _ref(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


def _commit_of(ref: RevisionRef) -> str | None:
    root = ref.revision_id.root
    return root if ref.digest == f"git-commit:{root}" else None


def _request(name: str = "r1", recipe: str = "validation/recipes.json") -> Validate:
    return Validate(revision=_ref(name), recipe_artifact=recipe)


def _recipe_file(root: Path, identity: str, *names: str) -> None:
    """Write the recipe file where the candidate for ``identity`` will be created."""
    candidate = root / member_workspace_id(f"validate-{identity[:32]}")
    (candidate / "validation").mkdir(parents=True, exist_ok=True)
    (candidate / "src.py").write_text("X = 1\n")
    recipes = [
        {
            "name": name,
            "command": f"echo {name}",
            "input_paths": ["src.py"],
            "timeout_seconds": 30,
            "purpose": "check the contract",
        }
        for name in names
    ]
    (candidate / "validation" / "recipes.json").write_text(
        json.dumps({"version": 1, "recipes": recipes})
    )


class _FakeLocalValidationEvents:
    """Records every recipe lifecycle observation `ValidateLocalOwner` publishes."""

    def __init__(self) -> None:
        self.started_recipes: list[ValidationRecipe] = []
        self.finished_results: list[FrameworkValidationResult] = []

    def started(self, recipe: ValidationRecipe) -> None:
        self.started_recipes.append(recipe)

    def finished(self, result: FrameworkValidationResult) -> None:
        self.finished_results.append(result)


def _owner(
    root: Path,
    retained: set[str],
    *,
    parallel: bool = True,
    events: _FakeLocalValidationEvents | None = None,
) -> tuple[ValidateLocalOwner, FakeWorkspaces, FakeCommands, _Store]:
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=root, known_revisions=retained), supports_parallel_candidates=parallel
    )
    commands = FakeCommands()
    store = _Store()
    owner = ValidateLocalOwner(
        workspaces,
        LocalValidator(commands, MemoryValidationPasses()),
        workspaces,
        _commit_of,
        cast("ArtifactStore", store),
        events=events,
    )
    return owner, workspaces, commands, store


async def test_passing_recipes_report_success_and_store_the_report(tmp_path: Path) -> None:
    owner, workspaces, commands, store = _owner(tmp_path, {"r1"})
    _recipe_file(tmp_path, "id", "unit", "smoke")
    commands.script(
        CommandResult(output="ok", exit_code=0), CommandResult(output="ok", exit_code=0)
    )
    outcome = await owner.validate(_request(), "id")
    assert outcome["status"] == "succeeded"
    assert outcome["passed"] is True
    assert outcome["recipe"] == ""
    (candidate,) = workspaces.candidates
    assert candidate.discarded
    assert [call.command for call in commands.trusted_shell_calls] == ["echo unit", "echo smoke"]
    (report,) = store.written
    assert [item["recipe"]["name"] for item in json.loads(report)["results"]] == ["unit", "smoke"]


async def test_an_injected_events_seam_observes_each_recipe_s_start_and_settled_result(
    tmp_path: Path,
) -> None:
    events = _FakeLocalValidationEvents()
    owner, _workspaces, commands, _store = _owner(tmp_path, {"r1"}, events=events)
    _recipe_file(tmp_path, "id", "unit", "smoke")
    commands.script(
        CommandResult(output="ok", exit_code=0), CommandResult(output="ok", exit_code=0)
    )

    outcome = await owner.validate(_request(), "id")

    assert outcome["passed"] is True
    assert [recipe.name for recipe in events.started_recipes] == ["unit", "smoke"]
    assert [result.recipe.name for result in events.finished_results] == ["unit", "smoke"]
    assert all(result.passed for result in events.finished_results)


async def test_with_no_injected_events_validation_still_succeeds(tmp_path: Path) -> None:
    """The events seam is optional: omitting it changes no observable behavior."""
    owner, _, commands, _ = _owner(tmp_path, {"r1"})
    _recipe_file(tmp_path, "id", "unit")
    commands.script(CommandResult(output="ok", exit_code=0))

    outcome = await owner.validate(_request(), "id")

    assert outcome["passed"] is True


async def test_the_first_failing_recipe_is_named_and_the_rest_do_not_run(tmp_path: Path) -> None:
    owner, _, commands, _ = _owner(tmp_path, {"r1"})
    _recipe_file(tmp_path, "id", "unit", "smoke")
    commands.script(CommandResult(output="boom", exit_code=2))
    outcome = await owner.validate(_request(), "id")
    assert (outcome["passed"], outcome["recipe"], outcome["output"]) == (False, "unit", "boom")
    assert len(commands.trusted_shell_calls) == 1


@pytest.mark.parametrize(
    "recipe", ["missing.json", "not a path", "../escape.json", "inline{}.json"]
)
async def test_a_recipe_the_agent_cannot_have_meant_is_its_input_error(
    tmp_path: Path, recipe: str
) -> None:
    owner, workspaces, commands, _ = _owner(tmp_path, {"r1"})
    _recipe_file(tmp_path, "id", "unit")
    outcome = await owner.validate(_request(recipe=recipe), "id")
    assert outcome["status"] == "succeeded"
    assert outcome["recipe_unusable"] is True
    assert outcome["passed"] is False
    assert not commands.trusted_shell_calls
    assert all(candidate.discarded for candidate in workspaces.candidates)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(retained=st.sets(st.sampled_from(["r1", "r2"])), asked=st.sampled_from(["r1", "r2", "x"]))
async def test_only_a_retained_revision_is_validated_and_its_candidate_is_always_discarded(
    tmp_path: Path, retained: set[str], asked: str
) -> None:
    root = tmp_path / f"{'-'.join(sorted(retained))}.{asked}"
    root.mkdir(parents=True, exist_ok=True)
    owner, workspaces, commands, _ = _owner(root, retained)
    _recipe_file(root, "id", "unit")
    commands.script(CommandResult(output="ok", exit_code=0))
    outcome = await owner.validate(_request(asked), "id")
    if asked not in retained:
        assert outcome["status"] == "rejected"
        assert not workspaces.candidates
        assert not commands.trusted_shell_calls
        return
    assert outcome["passed"] is True
    assert all(candidate.discarded for candidate in workspaces.candidates)


async def test_a_command_that_cannot_run_is_a_failed_recipe_and_the_candidate_is_discarded(
    tmp_path: Path,
) -> None:
    owner, workspaces, commands, store = _owner(tmp_path, {"r1"})
    _recipe_file(tmp_path, "id", "unit")
    commands.script(RuntimeError("sandbox lost"))
    outcome = await owner.validate(_request(), "id")
    assert outcome["passed"] is False
    assert "sandbox lost" in str(outcome["error"])
    assert workspaces.candidates[0].discarded
    assert store.written


async def test_a_run_without_isolated_candidates_refuses_instead_of_touching_the_root(
    tmp_path: Path,
) -> None:
    owner, workspaces, commands, _ = _owner(tmp_path, {"r1"}, parallel=False)
    outcome = await owner.validate(_request(), "id")
    assert outcome["status"] == "rejected"
    assert not workspaces.candidates
    assert not commands.trusted_shell_calls


async def test_inspection_never_runs_a_recipe(tmp_path: Path) -> None:
    owner, _, commands, _ = _owner(tmp_path, {"r1"})
    context = ExecutionContext(
        now_at=0.0,
        payload_digest="d",
        until=Until(at=100.0),
    )
    assert isinstance(await owner.inspect(_request(), context), NotApplied)
    assert not commands.trusted_shell_calls


def _prepare(root: Path, undeclared: str = "") -> None:
    """A workspace whose recipe declares ``src.py`` only; ``undeclared`` is a file it omits."""
    (root / "validation").mkdir(parents=True, exist_ok=True)
    (root / "src.py").write_text("X = 1\n")
    (root / "undeclared.txt").write_text(undeclared)
    recipe = {
        "name": "unit",
        "command": "echo unit",
        "input_paths": ["src.py"],
        "timeout_seconds": 30,
        "purpose": "check the contract",
    }
    (root / "validation" / "recipes.json").write_text(
        json.dumps({"version": 1, "recipes": [recipe]})
    )


async def _ran(
    validator: LocalValidator, commands: FakeCommands, workspace_id: str, root: Path
) -> bool:
    """Validate the workspace; whether the recipe command actually ran."""
    commands.script(CommandResult(output="ok", exit_code=0))
    before = len(commands.trusted_shell_calls)
    results = await validator.run(
        FakeWorkspace(path=root, workspace_id=workspace_id),
        recipe_artifact="validation/recipes.json",
        report_location="report.json",
    )
    assert all(item.passed for item in results)
    return len(commands.trusted_shell_calls) > before


@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    edited=st.text(alphabet="abc", max_size=6),
    ids=st.lists(st.sampled_from(["w1", "w2", "w3"]), min_size=2, max_size=2, unique=True),
)
async def test_a_pass_is_never_reused_by_a_workspace_at_another_revision(
    tmp_path: Path, edited: str, ids: list[str]
) -> None:
    """Candidates differing only in an undeclared file must each run the recipe.

    The agent declares ``input_paths``; a pass keyed by them alone would let a candidate
    that edited anything else skip validation by reusing another candidate's pass.
    """
    base = tmp_path / f"{ids[0]}-{ids[1]}-{edited or 'none'}"
    first, second = base / "a", base / "b"
    _prepare(first)
    _prepare(second, undeclared=edited)
    commands = FakeCommands()
    validator = LocalValidator(commands, MemoryValidationPasses())
    assert await _ran(validator, commands, ids[0], first)
    assert await _ran(validator, commands, ids[1], second)


async def test_a_pass_is_reused_by_the_same_revision(tmp_path: Path) -> None:
    _prepare(tmp_path / "a")
    _prepare(tmp_path / "b")
    commands = FakeCommands()
    validator = LocalValidator(commands, MemoryValidationPasses())
    assert await _ran(validator, commands, "w1", tmp_path / "a")
    assert not await _ran(validator, commands, "w1", tmp_path / "b")
