"""Public contract tests for trusted local-validation runtime mechanics."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import CommandResult
from vs_runtime.api.infrastructure import (
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    run_local_validation,
)
from vs_runtime.api.testing import FakeCommands, FakeWorkspace

if TYPE_CHECKING:
    from pathlib import Path


def _write_recipe(
    root: Path,
    *,
    name: str = "focused",
    command: str = "python -m pytest -q test_queue.py",
    input_path: str = "queue.py",
) -> None:
    (root / input_path).write_text("VALUE = 1\n")
    (root / "validation").mkdir()
    (root / "validation" / "recipes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": name,
                        "command": command,
                        "input_paths": [input_path],
                        "timeout_seconds": 37,
                        "purpose": "exercise the candidate contract",
                    }
                ],
            }
        )
    )


def test_executes_trusted_recipe_and_reuses_exact_pass(tmp_path: Path) -> None:
    async def exercise() -> tuple[FakeCommands, FakeCommands, dict[str, object]]:
        _write_recipe(tmp_path)
        workspace = FakeWorkspace(path=tmp_path)
        commands = FakeCommands()
        commands.script(CommandResult(output="passed\n", exit_code=0))
        first = await run_local_validation(
            commands,
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-1.json",
        )
        reused_commands = FakeCommands()
        second = await run_local_validation(
            reused_commands,
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-2.json",
        )
        assert first[0].passed
        assert second[0].reused
        payload = json.loads((tmp_path / "validation" / "report-2.json").read_text())
        return commands, reused_commands, payload

    commands, reused_commands, payload = asyncio.run(exercise())
    assert commands.trusted_shell_calls[0].command == "python -m pytest -q test_queue.py"
    assert commands.trusted_shell_calls[0].timeout_seconds == 37
    assert reused_commands.trusted_shell_calls == []
    results = payload["results"]
    assert isinstance(results, list)
    result = results[0]
    assert isinstance(result, dict)
    assert result["reused"] is True


def test_workspace_mutation_fails_recipe_and_restores_entry_revision(tmp_path: Path) -> None:
    async def exercise() -> tuple[FakeWorkspace, str]:
        _write_recipe(tmp_path)
        workspace = FakeWorkspace(path=tmp_path)
        workspace.script_pending_changes(["queue.py"])
        commands = FakeCommands()
        commands.script(CommandResult(output="passed", exit_code=0))
        run = await run_local_validation(
            commands,
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
        return workspace, run[0].error or ""

    workspace, error = asyncio.run(exercise())
    assert error == "validation command mutated the workspace: queue.py"
    assert workspace.restore_calls == [("fake-revision-1", True)]


def test_changed_declared_input_prevents_pass_reuse(tmp_path: Path) -> None:
    async def exercise() -> FakeCommands:
        _write_recipe(tmp_path)
        workspace = FakeWorkspace(path=tmp_path)
        first_commands = FakeCommands()
        first_commands.script(CommandResult(output="first", exit_code=0))
        await run_local_validation(
            first_commands,
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-1.json",
        )
        (tmp_path / "queue.py").write_text("VALUE = 2\n")
        second_commands = FakeCommands()
        second_commands.script(CommandResult(output="second", exit_code=0))
        await run_local_validation(
            second_commands,
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-2.json",
        )
        return second_commands

    commands = asyncio.run(exercise())
    assert len(commands.trusted_shell_calls) == 1


def test_cancellation_restores_entry_revision_and_writes_no_report(tmp_path: Path) -> None:
    async def exercise() -> FakeWorkspace:
        _write_recipe(tmp_path)
        workspace = FakeWorkspace(path=tmp_path)
        commands = FakeCommands()
        commands.script(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await run_local_validation(
                commands,
                workspace,
                recipe_artifact="validation/recipes.json",
                report_location="validation/report.json",
            )
        return workspace

    workspace = asyncio.run(exercise())
    assert workspace.restore_calls == [("fake-revision-1", True)]
    assert not (tmp_path / "validation" / "report.json").exists()


def test_rejects_duplicate_recipe_names_before_execution(tmp_path: Path) -> None:
    _write_recipe(tmp_path)
    artifact = tmp_path / "validation" / "recipes.json"
    payload = json.loads(artifact.read_text())
    payload["recipes"].append(payload["recipes"][0])
    artifact.write_text(json.dumps(payload))

    async def exercise() -> None:
        with pytest.raises(LocalValidationRecipeError) as raised:
            await run_local_validation(
                FakeCommands(),
                FakeWorkspace(path=tmp_path),
                recipe_artifact="validation/recipes.json",
                report_location="validation/report.json",
            )
        assert raised.value.kind is LocalValidationRecipeErrorKind.DUPLICATE_NAMES

    asyncio.run(exercise())


def test_failed_report_replace_removes_temporary_artifact(tmp_path: Path) -> None:
    async def exercise() -> None:
        _write_recipe(tmp_path)
        report = tmp_path / "validation" / "report.json"
        report.mkdir()
        commands = FakeCommands()
        commands.script(CommandResult(output="passed", exit_code=0))
        with pytest.raises(IsADirectoryError):
            await run_local_validation(
                commands,
                FakeWorkspace(path=tmp_path),
                recipe_artifact="validation/recipes.json",
                report_location="validation/report.json",
            )

    asyncio.run(exercise())
    assert list((tmp_path / "validation").glob(".report.json.*")) == []


def test_parallel_runs_keep_workspace_identity_and_reports_separate(tmp_path: Path) -> None:
    async def exercise() -> None:
        async def run_one(index: int) -> tuple[FakeCommands, FakeWorkspace]:
            root = tmp_path / str(index)
            root.mkdir()
            _write_recipe(root, command=f"check-{index}")
            workspace = FakeWorkspace(path=root, workspace_id=f"candidate-{index}")
            commands = FakeCommands()
            commands.script(CommandResult(output=f"ok-{index}", exit_code=0))
            await run_local_validation(
                commands,
                workspace,
                recipe_artifact="validation/recipes.json",
                report_location="validation/report.json",
            )
            return commands, workspace

        runs = await asyncio.gather(*(run_one(index) for index in range(20)))
        for index, (commands, workspace) in enumerate(runs):
            assert commands.trusted_shell_calls[0].workspace is workspace
            report = json.loads((tmp_path / str(index) / "validation" / "report.json").read_text())
            assert report["results"][0]["output"] == f"ok-{index}"

    asyncio.run(exercise())
