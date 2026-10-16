"""Configuration failures reach ``vibesys preflight`` as typed causes the desktop app can word.

Each failure a run's configuration can hit is a fact (``vibesys.api.ConfigurationCause``): the CLI
words it with its flags, ``vibesys preflight`` reports it as the cause the app words for its own
controls, and a failure with no fact falls back to the stage's class or to none.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING, Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.fake_github import signed_in_github, signed_out_github
from tests.support.task_project import write_task

from entrypoints import cli
from entrypoints.cli.preflight import preflight_report
from entrypoints.cli.preflight_cause import settings_cause, wire_cause
from server.host_preflight import (
    ArgumentsRejected,
    ArgumentsUnrecognized,
    Check,
    CheckId,
    CheckState,
    ConfigInvalid,
    GitHubUnavailable,
    ModelNotVetted,
    PreflightReport,
    RolesNotDeclared,
    SettingsInvalid,
    SettingsValid,
    TaskNeedsProvisioning,
    TaskNotFound,
    TaskUnusable,
)
from vibesys.api import (
    ConfigurationCause,
    ConfigurationDiagnostic,
    ConfigurationError,
    cli_wording,
    configuration_error,
)
from vibesys.api import (
    ModelNotVetted as ModelNotVettedFact,
)
from vibesys.api import (
    TaskNotFound as TaskNotFoundFact,
)
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from argparse import Namespace
    from pathlib import Path

    from tests.support.fake_github import FakeGitHub

_NAME = st.from_regex(r"[a-z][a-z0-9_.-]{0,12}", fullmatch=True)
_TEXT = st.text(min_size=1, max_size=60).filter(lambda text: text.strip() != "")


def _facts() -> dict[type[Any], st.SearchStrategy[Any]]:
    """A strategy per member of the closed fact set, built from each fact's own fields."""
    members = get_args(ConfigurationCause)
    strategies: dict[type[Any], st.SearchStrategy[Any]] = {}
    for member in members:
        assert is_dataclass(member)
        strategies[member] = st.builds(member)
    return strategies


@given(cause=st.one_of(*_facts().values()))
def test_every_fact_is_raised_as_an_error_with_its_code_stage_and_command_line_wording(
    cause: ConfigurationCause,
) -> None:
    error = configuration_error(cause)

    diagnostic = error.diagnostic
    assert diagnostic.cause == cause
    assert (diagnostic.code, diagnostic.stage) == (cause.code, cause.stage)
    assert diagnostic.message == cli_wording(cause)
    assert str(error) == diagnostic.message


@given(cause=st.one_of(*_facts().values()))
def test_every_fact_is_reported_as_one_wire_cause_that_round_trips(
    cause: ConfigurationCause,
) -> None:
    wired = wire_cause(cause)
    settings = SettingsInvalid(code=cause.code, stage=cause.stage, message="m", cause=wired)

    assert PreflightReport.model_validate_json(_report(settings).model_dump_json()).settings == (
        settings
    )
    # The wire cause carries every field the fact does.
    for field in fields(cause):
        assert hasattr(wired, field.name)


def _report(settings: SettingsInvalid | SettingsValid) -> PreflightReport:
    return PreflightReport(
        settings=settings,
        uses_docker=None,
        agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.WARN, detail="not checked"),
        evaluator=Check(
            id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.WARN, detail="not checked"
        ),
    )


def test_the_command_line_still_words_the_facts_with_the_flags_it_takes() -> None:
    unvetted = ModelNotVettedFact(
        provider="codex", model="foo", vetted=("gpt-a",), role="judge", fallback=False
    )

    assert cli_wording(unvetted) == (
        "agent.roles.judge.model: model 'foo' is not vetted for provider 'codex'; "
        "vetted models: gpt-a. Choose a vetted model, or pass --allow-unvetted-model "
        "at the terminal to run it anyway"
    )
    assert cli_wording(TaskNotFoundFact(task="x", available=("a", "b"))) == (
        "VibeSys task 'x' was not found. Available tasks: a, b"
    )


@given(stage=st.sampled_from(["argument_parsing", "semantic_validation"]), message=_TEXT)
def test_an_untyped_failure_at_an_argument_stage_is_reported_as_rejected_arguments(
    stage: str, message: str
) -> None:
    diagnostic = ConfigurationDiagnostic(code="x", stage=stage, message=message)

    assert settings_cause(diagnostic) == ArgumentsRejected(problem=message)


@given(
    stage=st.sampled_from(["input_loading", "repository_setup", "resume_resolution", "anything"]),
    message=_TEXT,
)
def test_an_untyped_failure_elsewhere_has_no_cause(stage: str, message: str) -> None:
    assert settings_cause(ConfigurationDiagnostic(code="x", stage=stage, message=message)) is None


def _project(root: Path, extra_manifest: str = "") -> Path:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    write_task(
        project,
        f"""\
version = 1
backends = ["cpu"]

[agent]
domain = "generic"

[accuracy]
command = ["python", "-c", "print('ok')"]

[benchmark]
command = ["python", "-c", "print('1')"]
{extra_manifest}""",
    )
    return project


def _parse(argv: list[str]) -> Namespace:
    return cli.parse_cli_invocation(argv).args


def _report_for(
    project: Path, *flags: str, github: FakeGitHub | None = None
) -> tuple[PreflightReport, FakeGitHub]:
    github = signed_in_github() if github is None else github
    argv = ["--project", str(project), "--task", "default", "--no-skills", "--profiler", "none"]
    report = preflight_report(
        [*argv, *flags], _parse, ScriptedProbe(lambda _argv: None), github=github.cli
    )
    return report, github


def _cause_of(report: PreflightReport) -> object:
    assert isinstance(report.settings, SettingsInvalid)
    return report.settings.cause


def test_a_task_the_project_lacks_is_reported_with_the_tasks_it_has(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report, _ = _report_for(project, "--task", "nosuch")

    assert _cause_of(report) == TaskNotFound(task="nosuch", available=("default",))


def test_a_task_whose_files_cannot_be_loaded_is_reported_with_the_loaders_account(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    (project / ".vibesys" / "tasks" / "default" / "vibesys.input.toml").write_text("version = ")

    report, _ = _report_for(project)

    cause = _cause_of(report)
    assert isinstance(cause, TaskUnusable)
    assert cause.task == "default"
    assert cause.problem


def test_a_task_that_provisions_sources_cannot_run_in_the_project_itself(tmp_path: Path) -> None:
    """Regression: a direct run of such a task failed at Start, naming a flag the app lacks."""
    project = _project(
        tmp_path,
        """
[[workspace.sources]]
name = "src"
repo = "https://example.com/r.git"
commit = "0123456789abcdef0123456789abcdef01234567"
dest = "d"
""",
    )

    report, _ = _report_for(project)

    assert _cause_of(report) == TaskNeedsProvisioning(task="default")


def test_rounds_for_a_search_without_rounds_are_unrecognized_arguments(tmp_path: Path) -> None:
    """Regression: the run's headline told a desktop user to type a flag."""
    project = _project(tmp_path)

    report, _ = _report_for(project, "--outer-loop", "evolve", "--max-rounds", "3")

    assert _cause_of(report) == ArgumentsUnrecognized(arguments=("--max-rounds", "3"))


def test_rounds_the_orchestration_rejects_are_a_failed_setting_not_a_crash(tmp_path: Path) -> None:
    """Regression: ``--max-rounds 0`` made ``vibesys preflight`` die with a pydantic traceback."""
    project = _project(tmp_path)

    report, _ = _report_for(project, "--max-rounds", "0")

    cause = _cause_of(report)
    assert isinstance(cause, ArgumentsRejected)
    assert "max_rounds" in cause.problem
    assert "Traceback" not in cause.problem


def test_a_role_the_orchestration_does_not_declare_is_reported_by_name(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report, _ = _report_for(project, "--role-model", "nosuch=gpt-a")

    assert _cause_of(report) == RolesNotDeclared(roles=("nosuch",))


def test_an_unvetted_role_model_is_reported_with_the_models_that_are_vetted(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    report, _ = _report_for(project, "--role-model", "multi-judge=not-a-model")

    cause = _cause_of(report)
    assert isinstance(cause, ModelNotVetted)
    assert (cause.role, cause.model, cause.fallback) == ("multi-judge", "not-a-model", False)
    assert cause.vetted
    assert "not-a-model" not in cause.vetted


def test_a_configuration_file_that_is_not_valid_is_reported_with_the_validations_account(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    config = tmp_path / "agent.toml"
    config.write_text('[run]\nmax_run_seconds = "soon"\n')

    report, _ = _report_for(project, "--config", str(config))

    cause = _cause_of(report)
    assert isinstance(cause, ConfigInvalid)
    assert "max_run_seconds" in cause.problem


@pytest.mark.parametrize(
    ("flags", "asks_github"),
    [
        (["--local"], False),
        ([], False),
        (["--publish"], True),
        (["--repo", "trial"], True),
    ],
    ids=["local", "direct-run", "publish", "named-repository"],
)
def test_a_run_that_publishes_needs_a_github_account_and_one_that_does_not_never_asks(
    tmp_path: Path, flags: list[str], *, asks_github: bool
) -> None:
    """Regression: Start failed on a missing ``gh`` account that preflight never looked for."""
    project = _project(tmp_path)

    report, github = _report_for(project, *flags, github=signed_out_github("gh: not signed in"))

    assert github.asked is asks_github
    if asks_github:
        cause = _cause_of(report)
        assert isinstance(cause, GitHubUnavailable)
        assert "gh: not signed in" in cause.problem
    else:
        assert isinstance(report.settings, SettingsValid)


def test_a_run_that_publishes_with_an_account_passes(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report, _ = _report_for(project, "--publish")

    assert isinstance(report.settings, SettingsValid)


def test_a_failure_is_one_error_whose_diagnostic_carries_its_fact() -> None:
    cause = TaskNotFoundFact(task="x", available=())

    with pytest.raises(ConfigurationError) as raised:
        raise configuration_error(cause)

    assert raised.value.diagnostic.cause == cause
