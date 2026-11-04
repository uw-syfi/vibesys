"""Task commands survive lowering into the trusted runtime evaluation plan."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.task_project import write_task

from vibesys.inputs import load_task
from vibesys.run.evaluation import trusted_evaluation_plan

if TYPE_CHECKING:
    from pathlib import Path


def test_the_task_profile_command_reaches_the_trusted_evaluation_plan(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
        '[profile]\ncommand = ["python", "profile.py", "--label", "queue run"]\n'
        "timeout_seconds = 37\n",
    )

    plan = trusted_evaluation_plan(
        load_task(tmp_path),
        accuracy_command="trusted-accuracy",
        benchmark_command="trusted-bench",
        framework_setup_timeout_seconds=7,
        candidate_startup_causes=(),
    )

    assert plan.profile_command == "python profile.py --label 'queue run'"
    assert plan.profile_timeout_seconds == 37
    assert (plan.accuracy_command, plan.benchmark_command) == ("trusted-accuracy", "trusted-bench")
    assert plan.framework_setup_timeout_seconds == 7
