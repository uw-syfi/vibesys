"""The typed causes ``run.preflight`` reports for failed settings: closed, gated, and flag-free."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.server.host_helper_support import FakeRunPreflight, fake_host

from server.host_api import (
    MAX_ROUNDS_FLAG,
    HelperFeature,
    RunPreflightReply,
    RunPreflightRequest,
    request_features,
)
from server.host_compute import ModalTarget, SameMachine, SlurmTarget
from server.host_helper_preflight import (
    PreflightFacts,
    preflight_checks,
    refined_cause,
    typed_arguments,
)
from server.host_helper_requests import answer
from server.host_machine_facts import (
    Accelerator,
    DockerReachable,
)
from server.host_preflight import (
    ArgumentsRejected,
    ArgumentsUnrecognized,
    Backend,
    BackendUnavailable,
    Cause,
    Check,
    CheckId,
    CheckState,
    ConfigInvalid,
    GitHubUnavailable,
    MeasurementTargetMisfit,
    MeasurementTargetNotConfigured,
    ModelNotVetted,
    OptionNotAccepted,
    PreflightReport,
    ProjectDirty,
    ReasoningEffort,
    RoleOption,
    RolesNotDeclared,
    RunOption,
    RunOptions,
    RunResults,
    SettingsInvalid,
    SettingsValid,
    SysboxMissing,
    TaskImageUnsupported,
    TaskNeedsProvisioning,
    TaskNotFound,
    TaskUnusable,
    cause_text,
)
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path as PathType

_NAME = st.from_regex(r"[a-z][a-z0-9_.-]{0,12}", fullmatch=True)
_PATH = st.from_regex(r"[a-z][a-z0-9_./]{0,20}", fullmatch=True)
_RAW_MARK = "(the run's own words)"
_RAW = st.text(min_size=1, max_size=40).map(lambda text: f"--{text} {_RAW_MARK}")
"""Text a run prints about a failure: it names flags, as the run's messages do."""

_MESSAGE = "the run's own message --flag"

_CAUSES: dict[type[Any], st.SearchStrategy[Cause]] = {
    BackendUnavailable: st.builds(
        BackendUnavailable,
        task=_NAME,
        backend=st.sampled_from(Backend),
        declared=st.lists(st.sampled_from(Backend), unique=True, max_size=3).map(tuple),
    ),
    ProjectDirty: st.builds(
        ProjectDirty, project=_PATH, paths=st.lists(_PATH, min_size=1, max_size=4).map(tuple)
    ),
    TaskNotFound: st.builds(
        TaskNotFound, task=_NAME, available=st.lists(_NAME, max_size=4).map(tuple)
    ),
    TaskUnusable: st.builds(TaskUnusable, task=st.none() | _NAME, problem=_RAW),
    TaskNeedsProvisioning: st.builds(TaskNeedsProvisioning, task=_NAME),
    TaskImageUnsupported: st.builds(TaskImageUnsupported, task=st.none() | _NAME, problem=_RAW),
    SysboxMissing: st.builds(SysboxMissing, task=_NAME, problem=_RAW),
    ConfigInvalid: st.builds(ConfigInvalid, problem=_RAW),
    RolesNotDeclared: st.builds(
        RolesNotDeclared, roles=st.lists(_NAME, min_size=1, max_size=3).map(tuple)
    ),
    ModelNotVetted: st.builds(
        ModelNotVetted,
        provider=_NAME,
        model=st.none() | _NAME,
        vetted=st.lists(_NAME, max_size=4).map(tuple),
        role=st.none() | _NAME,
        fallback=st.booleans(),
    ),
    ArgumentsRejected: st.builds(ArgumentsRejected, problem=_RAW),
    ArgumentsUnrecognized: st.builds(
        ArgumentsUnrecognized, arguments=st.lists(_RAW, min_size=1, max_size=3).map(tuple)
    ),
    OptionNotAccepted: st.builds(OptionNotAccepted, option=st.sampled_from(RunOption)),
    GitHubUnavailable: st.builds(GitHubUnavailable, problem=_RAW),
    MeasurementTargetNotConfigured: st.builds(
        MeasurementTargetNotConfigured,
        target=st.builds(SlurmTarget, cluster=_NAME) | st.just(ModalTarget()),
    ),
    MeasurementTargetMisfit: st.builds(
        MeasurementTargetMisfit,
        target=st.just(SameMachine())
        | st.builds(SlurmTarget, cluster=_NAME)
        | st.just(ModalTarget()),
        backend=st.sampled_from(Backend),
        offered=st.sampled_from(Accelerator),
    ),
}
_ANY_CAUSE = st.one_of(*_CAUSES.values())


def test_every_member_of_the_closed_cause_set_has_a_strategy() -> None:
    """A cause added to the contract without a strategy here fails, so no property skips it."""
    members = set(get_args(get_args(Cause)[0]))

    assert set(_CAUSES) == members


def _invalid(cause: Cause | None, message: str = _MESSAGE) -> PreflightReport:
    return PreflightReport(
        settings=SettingsInvalid(code="x", stage="y", message=message, cause=cause),
        uses_docker=None,
        agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.WARN, detail="not checked"),
        evaluator=Check(
            id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.WARN, detail="not checked"
        ),
    )


_FACTS = PreflightFacts(
    project_root=Path("/work/proj"),
    repository_root=None,
    checkout=None,
    vibesys_version="0.1.1",
    docker=DockerReachable(version="27"),
)


def _settings(
    cause: Cause | None, *, with_causes: bool, options: RunOptions | None = None
) -> Check:
    checks = preflight_checks(_FACTS, _invalid(cause), options=options, with_causes=with_causes)
    (check,) = [check for check in checks if check.id is CheckId.SETTINGS_VALID]
    return check


@pytest.mark.parametrize("option", list(RunOption))
def test_every_typed_option_is_worded_in_the_cause_that_refuses_it(option: RunOption) -> None:
    text = cause_text(OptionNotAccepted(option=option))

    assert "_" not in text


@given(cause=_ANY_CAUSE)
def test_every_cause_round_trips_through_the_report_and_the_check(cause: Cause) -> None:
    report = _invalid(cause)
    check = _settings(cause, with_causes=True)

    assert PreflightReport.model_validate_json(report.model_dump_json()) == report
    assert Check.model_validate_json(check.model_dump_json()) == check
    assert check.cause == cause


@given(cause=_ANY_CAUSE)
def test_no_cause_is_worded_with_a_flag_or_the_runs_raw_text(cause: Cause) -> None:
    """Regression: the headline of a failed setting was the CLI's first error line, flags and all."""
    line = cause_text(cause)
    check = _settings(cause, with_causes=True)

    assert "\n" not in line
    for text in (line, check.detail):
        assert _RAW_MARK not in text
        if not isinstance(cause, ProjectDirty):  # its detail names a path, which is not a flag
            assert "--" not in text


@given(cause=_ANY_CAUSE)
def test_a_client_that_did_not_ask_for_causes_keeps_the_runs_message_and_no_new_cause(
    cause: Cause,
) -> None:
    check = _settings(cause, with_causes=False)

    if isinstance(cause, BackendUnavailable):
        # Gated by its own feature (run_backend), and flag-free before run_causes existed.
        assert check.cause == cause
        assert "--" not in check.detail
    else:
        assert check.cause is None
        assert check.detail == _MESSAGE


@pytest.mark.parametrize("asked", [True, False])
def test_a_failure_with_no_cause_keeps_the_runs_message_whoever_asks(*, asked: bool) -> None:
    check = _settings(None, with_causes=asked)

    assert (check.state, check.cause, check.detail) == (CheckState.FAIL, None, _MESSAGE)


_ROLES = st.builds(
    RoleOption,
    role=_NAME,
    model=st.none() | _NAME,
    reasoning_effort=st.sampled_from(ReasoningEffort),
)
_OPTIONS = st.builds(
    RunOptions,
    rounds=st.none() | st.integers(1, 99),
    time_limit_s=st.none() | st.integers(1, 10**6),
    results=st.none() | st.sampled_from(RunResults),
    roles=st.lists(_ROLES, max_size=3, unique_by=lambda option: option.role).map(tuple),
)
_STRANGERS = st.lists(_NAME.map(lambda name: f"--stranger-{name}"), max_size=3)


@given(options=_OPTIONS, strangers=_STRANGERS)
def test_a_rejected_flag_a_typed_option_wrote_is_traced_back_to_that_option(
    options: RunOptions, strangers: list[str]
) -> None:
    """The run reports the arguments its search does not take; the screen names the control."""
    for option, arguments in typed_arguments(options):
        rejected = ArgumentsUnrecognized(arguments=(*strangers, *arguments))

        assert refined_cause(rejected, options) == OptionNotAccepted(option=option)


@given(options=_OPTIONS, strangers=_STRANGERS.filter(bool))
def test_arguments_no_typed_option_wrote_stay_unrecognized_arguments(
    options: RunOptions, strangers: list[str]
) -> None:
    rejected = ArgumentsUnrecognized(arguments=tuple(strangers))

    assert refined_cause(rejected, options) == rejected


def test_rounds_for_a_search_without_rounds_is_reported_as_the_rounds_option() -> None:
    check = _settings(
        ArgumentsUnrecognized(arguments=(MAX_ROUNDS_FLAG, "3")),
        with_causes=True,
        options=RunOptions(rounds=3),
    )

    assert check.cause == OptionNotAccepted(option=RunOption.ROUNDS)
    assert check.detail == "this search does not take the rounds setting"


def test_a_cause_other_than_unrecognized_arguments_is_not_refined() -> None:
    cause = ArgumentsRejected(problem="argument --max-rounds: invalid int value: 'x'")

    assert refined_cause(cause, RunOptions(rounds=3)) == cause


def test_asking_for_causes_needs_the_run_causes_feature() -> None:
    request = RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": "/p", "with_causes": True}
    )

    assert request_features(request) == (HelperFeature.PREFLIGHT, HelperFeature.RUN_CAUSES)


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize("asked", [None, True])
def test_the_reply_carries_a_settings_cause_exactly_when_it_was_asked_for(
    tmp_path: PathType, *, asked: bool | None
) -> None:
    cause = TaskNotFound(task="nosuch", available=("spsc",))
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        preflight=FakeRunPreflight(report_=_invalid(cause, "VibeSys task 'nosuch' was not found")),
    )
    request = RunPreflightRequest.model_validate(
        {
            "request_id": "p",
            "deadline_ms": 5000,
            "project": str(_project(tmp_path / "proj")),
            "task": "nosuch",
            "with_causes": asked,
        }
    )

    reply = answer(request, host, threads.event())

    assert isinstance(reply, RunPreflightReply)
    settings = next(check for check in reply.checks if check.id is CheckId.SETTINGS_VALID)
    assert settings.state is CheckState.FAIL
    assert settings.cause == (cause if asked else None)
    assert ('"cause"' in reply.model_dump_json()) is bool(asked)


def test_a_valid_report_has_no_cause_to_report() -> None:
    report = _invalid(None).model_copy(update={"settings": SettingsValid()})

    checks = preflight_checks(_FACTS, report, with_causes=True)

    (settings,) = [check for check in checks if check.id is CheckId.SETTINGS_VALID]
    assert (settings.state, settings.cause) == (CheckState.OK, None)
