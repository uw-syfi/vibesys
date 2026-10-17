"""Task commands survive lowering into the trusted runtime evaluation plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.task_project import write_task

from vibesys.inputs import load_task
from vibesys.run.evaluation import trusted_evaluation_plan
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView
from vs_sandbox.api.testing import FakeCommandRunner

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.api import CommandRunner


@dataclass
class _Session:
    """The run-environment facts consumed by evaluation-plan lowering."""

    sandbox: CommandRunner = field(default_factory=FakeCommandRunner)
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(
            paths=AgentPaths(
                accuracy_command="trusted-accuracy", benchmark_command="trusted-bench"
            ),
            framework_setup_timeout_seconds=7,
        )
    )

    @property
    def capture_commands(self) -> CommandRunner:
        return self.sandbox

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def close(self) -> None:
        return None


def test_the_task_profile_command_reaches_the_trusted_evaluation_plan(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
        '[profile]\ncommand = ["python", "profile.py", "--label", "queue run"]\n'
        "timeout_seconds = 37\n",
    )

    plan = trusted_evaluation_plan(load_task(tmp_path), _Session(), candidate_startup_causes=())

    assert plan.profile_command == "python profile.py --label 'queue run'"
    assert plan.profile_timeout_seconds == 37
