"""Public contract tests for trusted local-validation runtime mechanics."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api import CommandResult
from vs_runtime.api.infrastructure import (
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    LocalValidator,
)
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, MemoryValidationPasses
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api import DISCARD
from vs_sim.api.testing import examples


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
        passes = MemoryValidationPasses()
        first = await LocalValidator(commands, passes).run(
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-1.json",
        )
        reused_commands = FakeCommands()
        second = await LocalValidator(reused_commands, passes).run(
            FakeWorkspace(path=tmp_path),
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


@pytest.mark.parametrize("split_at", range(1, len("API_TOKEN")))
def test_failed_recipe_uses_the_diagnostic_created_before_command_output_was_bounded(
    tmp_path: Path,
    split_at: int,
) -> None:
    async def exercise() -> tuple[str, str]:
        _write_recipe(tmp_path)
        credential = "API_TOKEN=validation-secret"
        suffix = "z" * (81 + split_at - len(credential) - 1)
        runner = FakeCommandRunner(max_output_chars=100)
        runner.script_process(
            "python -m pytest -q test_queue.py",
            stderr="p" * 32 + credential + " " + suffix,
            returncode=1,
        )
        bounded = runner.execute("python -m pytest -q test_queue.py", output=DISCARD)
        assert "validation-secret" in bounded.output
        commands = FakeCommands()
        commands.script(
            CommandResult(
                output=bounded.output,
                exit_code=bounded.exit_code,
                truncated=bounded.truncated,
                diagnostic_output=bounded.diagnostic_output,
            )
        )
        (result,) = await LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=tmp_path),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
        assert not result.passed
        assert result.operator_output is not None
        return result.output, result.operator_output

    output, operator_output = asyncio.run(exercise())

    assert "validation-secret" in output
    assert "validation-secret" not in operator_output
    assert len(output) <= 100
    assert len(operator_output) <= 100


def test_workspace_mutation_fails_recipe_and_restores_entry_revision(tmp_path: Path) -> None:
    async def exercise() -> tuple[FakeWorkspace, str]:
        _write_recipe(tmp_path)
        workspace = FakeWorkspace(path=tmp_path)
        workspace.script_pending_changes(["queue.py"])
        commands = FakeCommands()
        commands.script(CommandResult(output="passed", exit_code=0))
        run = await LocalValidator(commands, MemoryValidationPasses()).run(
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
        passes = MemoryValidationPasses()
        await LocalValidator(first_commands, passes).run(
            workspace,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-1.json",
        )
        (tmp_path / "queue.py").write_text("VALUE = 2\n")
        second_commands = FakeCommands()
        second_commands.script(CommandResult(output="second", exit_code=0))
        await LocalValidator(second_commands, passes).run(
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
            await LocalValidator(commands, MemoryValidationPasses()).run(
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
            await LocalValidator(FakeCommands(), MemoryValidationPasses()).run(
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
            await LocalValidator(commands, MemoryValidationPasses()).run(
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
            await LocalValidator(commands, MemoryValidationPasses()).run(
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


def _learn_pass_report(root: Path) -> str:
    """Run the recipe once in a scratch workspace and return the pass report it wrote."""
    root.mkdir()
    _write_recipe(root)
    commands = FakeCommands()
    commands.script(CommandResult(output="passed", exit_code=0))
    asyncio.run(
        LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=root),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
    )
    return (root / "validation" / "report.json").read_text()


def _validate_with_planted(root: Path, planted: dict[str, str]) -> tuple[FakeCommands, bool]:
    """Validate a workspace holding ``planted`` files, with no recorded passes; failing command."""
    root.mkdir()
    _write_recipe(root)
    for relative, contents in planted.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(contents)
    commands = FakeCommands()
    commands.script(CommandResult(output="failed", exit_code=1))
    (result,) = asyncio.run(
        LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=root),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-2.json",
        )
    )
    return commands, result.passed or result.reused


def test_a_pass_report_in_the_workspace_is_never_reused(tmp_path: Path) -> None:
    report = _learn_pass_report(tmp_path / "scratch")
    commands, trusted = _validate_with_planted(
        tmp_path / "candidate", {"validation/report-1.json": report}
    )
    assert len(commands.trusted_shell_calls) == 1
    assert not trusted


_REPORT_NAMES = st.from_regex(r"validation/[a-z0-9-]{1,12}\.json", fullmatch=True).filter(
    lambda name: name not in {"validation/recipes.json", "validation/report-2.json"}
)


@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@given(
    names=st.lists(_REPORT_NAMES, min_size=1, max_size=3, unique=True),
    reported_digest=st.booleans(),
    noise=st.text(max_size=40),
)
def test_no_workspace_file_turns_a_failing_recipe_into_a_pass(
    names: list[str], *, reported_digest: bool, noise: str
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload = json.loads(_learn_pass_report(root / "scratch"))
        if not reported_digest:
            payload["results"][0]["input_digest"] = noise
        planted = {name: json.dumps(payload) for name in names}
        commands, trusted = _validate_with_planted(root / "candidate", planted)
    assert len(commands.trusted_shell_calls) == 1
    assert not trusted


def test_a_declared_input_that_is_a_fifo_fails_the_recipe_without_blocking(
    tmp_path: Path,
) -> None:
    # Reading a FIFO blocks until a writer appears, and its stat size says nothing about
    # what a read returns: hashing must decide on the opened file, not on an earlier stat.
    _write_recipe(tmp_path)
    (tmp_path / "queue.py").unlink()
    os.mkfifo(tmp_path / "queue.py")

    async def exercise() -> tuple[str, FakeCommands]:
        commands = FakeCommands()
        run = await LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=tmp_path),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
        return run[0].error or "", commands

    error, commands = asyncio.run(exercise())
    assert "not a readable regular file" in error
    assert commands.trusted_shell_calls == []
