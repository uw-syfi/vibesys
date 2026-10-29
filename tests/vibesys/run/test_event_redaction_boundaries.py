"""Credential safety at the actual framework-event producer boundaries."""

import asyncio
import json
from pathlib import Path

import pytest

from vibesys.events import CoreEvent, CoreEventType, GateFinishedData
from vibesys.run.evaluation import CoreLocalValidationEvents
from vibesys.run.event_journal import EventJournal
from vs_runtime.api import CommandResult
from vs_runtime.api.infrastructure import (
    FrameworkValidationResult,
    LocalValidator,
    ValidationRecipe,
)
from vs_runtime.api.testing import FakeCommands, FakeWorkspace, MemoryValidationPasses
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api import DISCARD
from vs_sim.api.testing import ManualClock, SimThreads

_GATE_OUTPUT_LIMIT = 1000


@pytest.mark.parametrize("split_at", range(1, len("API_TOKEN")))
def test_local_validation_redacts_before_bounding_gate_output(
    tmp_path: Path, split_at: int
) -> None:
    credential = "API_TOKEN=gate-secret"
    suffix = "z" * (81 + split_at - len(credential) - 1)
    (tmp_path / "input.py").write_text("VALUE = 1\n")
    (tmp_path / "validation").mkdir()
    (tmp_path / "validation" / "recipes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "credential-boundary",
                        "command": "false",
                        "input_paths": ["input.py"],
                        "timeout_seconds": 30,
                        "purpose": "Exercise a failed validation result.",
                    }
                ],
            }
        )
    )
    runner = FakeCommandRunner(max_output_chars=100)
    runner.script_process(
        "false",
        stderr="p" * 32 + credential + " " + suffix,
        returncode=1,
    )
    bounded = runner.execute("false", output=DISCARD)
    commands = FakeCommands()
    commands.script(
        CommandResult(
            output=bounded.output,
            exit_code=bounded.exit_code,
            truncated=bounded.truncated,
            diagnostic_output=bounded.diagnostic_output,
        )
    )
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    captured: list[CoreEvent] = []
    journal.subscribe(captured.append)
    events = CoreLocalValidationEvents(journal)

    (result,) = asyncio.run(
        LocalValidator(commands, MemoryValidationPasses()).run(
            FakeWorkspace(path=tmp_path),
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
            events=events,
        )
    )

    assert "gate-secret" in result.output
    report = json.loads((tmp_path / "validation" / "report.json").read_text())
    assert "gate-secret" in report["results"][0]["output"]
    finished = next(event for event in captured if event.type is CoreEventType.GATE_FINISHED)
    assert isinstance(finished.data, GateFinishedData)
    assert finished.data.output_tail is not None
    assert len(finished.data.output_tail) <= _GATE_OUTPUT_LIMIT
    assert "gate-secret" not in finished.data.output_tail


def test_local_validation_preserves_an_explicitly_empty_safe_error() -> None:
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    captured: list[CoreEvent] = []
    journal.subscribe(captured.append)
    events = CoreLocalValidationEvents(journal)
    recipe = ValidationRecipe(
        name="empty-safe-error",
        command="false",
        input_paths=("input.py",),
        purpose="Exercise explicit empty operator diagnostics.",
    )

    events.finished(
        FrameworkValidationResult(
            recipe=recipe,
            input_digest="digest",
            passed=False,
            error="API_TOKEN=raw-agent-visible-secret",
            operator_error="",
            operator_output="must-not-replace-explicit-empty",
        )
    )

    finished = next(event for event in captured if event.type is CoreEventType.GATE_FINISHED)
    assert isinstance(finished.data, GateFinishedData)
    assert finished.data.output_tail == ""


@pytest.mark.parametrize(
    ("error", "output"),
    [
        ("CLIENTSECRET=error-secret", "must-not-replace-error"),
        (None, "PRIVATEKEY=output-secret"),
    ],
)
def test_local_validation_sanitizes_an_omitted_operator_projection(
    error: str | None, output: str
) -> None:
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    captured: list[CoreEvent] = []
    journal.subscribe(captured.append)
    recipe = ValidationRecipe(
        name="omitted-safe-error",
        command="false",
        input_paths=("input.py",),
        purpose="Exercise a legacy result without an operator projection.",
    )

    CoreLocalValidationEvents(journal).finished(
        FrameworkValidationResult(
            recipe=recipe,
            input_digest="digest",
            passed=False,
            error=error,
            output=output,
        )
    )

    finished = next(event for event in captured if event.type is CoreEventType.GATE_FINISHED)
    assert isinstance(finished.data, GateFinishedData)
    assert finished.data.output_tail is not None
    assert "secret" not in finished.data.output_tail
    assert "[REDACTED]" in finished.data.output_tail
