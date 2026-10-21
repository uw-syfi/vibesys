"""All launch entrypoints select saved strategy and execution settings on resume."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.task_project import write_task

from entrypoints.cli import build_run_request, parse_cli_invocation
from entrypoints.server import main as server_main
from vibesys.api import ConfigurationError
from vibesys.orchestration.dynamic.launch import DynamicLaunch
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record

_RUN = "20261021-170000-11111111-dynamic"


def saved_project(root: Path) -> Path:
    root.mkdir()
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        objective="Make the queue faster.\n",
    )
    store = Project.open(root).state
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store.create_project(root.name, now=now)
    store.create_run(
        store.new_run_manifest(
            root.name,
            run_id=_RUN,
            branch=f"vibesys-runs/{_RUN}",
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record().model_copy(update={"agent_backend": "scripted"}),
            orchestration=OrchestrationDescriptor(
                id="dynamic",
                config_version=1,
                options=DynamicLaunch(
                    interface="service",
                    max_rounds=17,
                    max_in_flight=3,
                ).model_dump(mode="json", exclude={"profiling"}),
            ),
            trusted_input_baseline="0" * 40,
            now=now,
        ),
        make_current=True,
    )
    return root


@pytest.mark.regression
@pytest.mark.parametrize("command", [[], ["relaunch"]])
def test_resume_selects_saved_strategy_before_parsing_its_options(
    tmp_path: Path, command: list[str]
) -> None:
    root = saved_project(tmp_path / "project")
    argv = [*command, "--project", str(root), "--resume", _RUN]
    invocation = parse_cli_invocation(argv)
    assert invocation.loop_kind == "dynamic"
    assert invocation.args.agent_backend == "scripted"
    assert invocation.args.max_rounds == 17
    assert invocation.args.max_in_flight == 3
    request = build_run_request(invocation)
    assert request.orchestration.id == "dynamic"
    assert request.resume is not None
    assert request.resume.run_id == _RUN


def test_explicit_strategy_or_execution_changes_still_fail(tmp_path: Path) -> None:
    root = saved_project(tmp_path / "project")
    for flags in (["--outer-loop", "agent"], ["--agent-backend", "cli"]):
        with pytest.raises(ConfigurationError, match=r"not agent|agent_backend"):
            parse_cli_invocation(["--project", str(root), "--resume", _RUN, *flags])


@pytest.mark.regression
def test_detached_relaunch_validates_saved_settings_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = saved_project(tmp_path / "project")
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    with pytest.raises(SystemExit) as raised:
        server_main(
            [
                "relaunch",
                "--project",
                str(root),
                "--resume",
                _RUN,
                "--detach",
                "--agent-backend",
                "cli",
            ]
        )
    assert raised.value.code == 2
    output = capsys.readouterr()
    assert "agent_backend" in output.err
    assert "unrecognized arguments" not in output.err
