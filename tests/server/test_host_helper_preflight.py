"""``run.preflight`` and ``run.start``'s typed options: closed checks, one meaning per option."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import (
    FakeRunLauncher,
    FakeRunPreflight,
    failed_launch,
    fake_host,
    quiet_machine,
)

from server.host_api import (
    LOCAL_FLAG,
    MAX_ROUNDS_FLAG,
    MAX_RUN_SECONDS_FLAG,
    PUBLISH_FLAG,
    REPO_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
    HarnessChoice,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    RunPreflightReply,
    RunPreflightRequest,
    RunStartRequest,
    request_features,
)
from server.host_helper_machine import DOCKER_VERSION_COMMAND
from server.host_helper_preflight import (
    PreflightFacts,
    option_arguments,
    option_flags,
    preflight_checks,
    repeated_option,
)
from server.host_helper_requests import answer, launch_arguments
from server.host_machine_facts import (
    DockerReachable,
    DockerUnreachable,
)
from server.host_preflight import (
    Backend,
    BackendUnavailable,
    Check,
    CheckId,
    CheckState,
    CopyOut,
    PreflightReport,
    ProjectDirty,
    ReasoningEffort,
    RoleOption,
    RunOptions,
    RunResults,
    SettingsInvalid,
    SettingsValid,
    StartDocker,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import SimNetwork, SimThreads

_ROLES = st.builds(
    RoleOption,
    role=st.sampled_from(["implementer", "planner", "reviewer"]),
    model=st.none() | st.sampled_from(["gpt-a", "gpt-b"]),
    reasoning_effort=st.sampled_from(ReasoningEffort),
)
_OPTIONS = st.builds(
    RunOptions,
    rounds=st.none() | st.integers(1, 99),
    time_limit_s=st.none() | st.integers(1, 10**6),
    results=st.none() | st.sampled_from(RunResults),
    roles=st.lists(_ROLES, max_size=3, unique_by=lambda option: option.role).map(tuple),
)
_FLAGS = (
    MAX_ROUNDS_FLAG,
    MAX_RUN_SECONDS_FLAG,
    LOCAL_FLAG,
    PUBLISH_FLAG,
    REPO_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_REASONING_EFFORT_FLAG,
)


def _values(arguments: tuple[str, ...], flag: str) -> list[str]:
    return [arguments[index + 1] for index, token in enumerate(arguments) if token == flag]


@given(_OPTIONS)
def test_each_option_set_becomes_its_flag_and_an_unset_one_none(options: RunOptions) -> None:
    arguments = option_arguments(options)

    assert _values(arguments, MAX_ROUNDS_FLAG) == (
        [] if options.rounds is None else [str(options.rounds)]
    )
    assert _values(arguments, MAX_RUN_SECONDS_FLAG) == (
        [] if options.time_limit_s is None else [str(options.time_limit_s)]
    )
    assert (LOCAL_FLAG in arguments) == (options.results is RunResults.KEEP)
    assert (PUBLISH_FLAG in arguments) == (options.results is RunResults.PUBLISH_GITHUB)
    assert _values(arguments, ROLE_MODEL_FLAG) == [
        f"{role.role}={role.model}" for role in options.roles if role.model is not None
    ]
    assert _values(arguments, ROLE_REASONING_EFFORT_FLAG) == [
        f"{role.role}={role.reasoning_effort}"
        for role in options.roles
        if role.reasoning_effort is not ReasoningEffort.DEFAULT
    ]


@given(_OPTIONS, st.sampled_from(_FLAGS), st.integers(3, 30), st.sampled_from(["", "=1"]))
def test_args_may_repeat_no_option_that_is_set(
    options: RunOptions, flag: str, prefix: int, value: str
) -> None:
    token = flag[: max(prefix, 3)] + value
    refusal = repeated_option(("--seed", "7", token), options)

    assert (refusal is not None) == any(
        candidate.startswith(token.split("=", 1)[0]) for candidate in option_flags(options)
    )
    if refusal is not None:
        assert token in refusal


def test_options_name_each_role_once() -> None:
    with pytest.raises(ValidationError, match="implementer"):
        RunOptions(roles=(RoleOption(role="implementer"), RoleOption(role="implementer")))


def test_run_start_launches_with_the_typed_options_before_its_args() -> None:
    request = RunStartRequest(
        request_id="s",
        deadline_ms=1,
        project="/p",
        args=("--seed", "7"),
        options=RunOptions(rounds=3, results=RunResults.KEEP),
    )
    assert launch_arguments(request) == (
        "--detach",
        MAX_ROUNDS_FLAG,
        "3",
        LOCAL_FLAG,
        "--seed",
        "7",
    )


_UNASKED_CHECKS = [check for check in CheckId if check is not CheckId.MEASUREMENT_TARGET]
"""The checks every reply holds in order; ``measurement_target`` only comes with ``measure_on``."""


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


def _preflight(
    project: Path, *, report: PreflightReport | None = None, **fields: object
) -> tuple[object, FakeRunPreflight]:
    threads = SimThreads()
    preflight = FakeRunPreflight() if report is None else FakeRunPreflight(report_=report)
    host = replace(fake_host(threads, SimNetwork(threads)), preflight=preflight)
    request = RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": str(project), **fields}
    )
    return answer(request, host, threads.event()), preflight


def test_preflight_runs_the_run_check_with_the_arguments_run_start_would_launch(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path / "proj")
    options = RunOptions(time_limit_s=600, roles=(RoleOption(role="implementer", model="gpt-a"),))

    reply, preflight = _preflight(
        project,
        task="spsc",
        harness={"provider": "codex", "model": "gpt-a"},
        options=options.model_dump(mode="json"),
    )

    assert isinstance(reply, RunPreflightReply)
    # Without `with_project_clean` the reply is the one a client that predates that check expects.
    assert [check.id for check in reply.checks] == [
        check_id for check_id in _UNASKED_CHECKS if check_id is not CheckId.PROJECT_CLEAN
    ]
    assert preflight.calls == [
        (
            project.resolve(),
            (
                "--task",
                "spsc",
                "--cli-provider",
                "codex",
                "--model",
                "gpt-a",
                MAX_RUN_SECONDS_FLAG,
                "600",
                ROLE_MODEL_FLAG,
                "implementer=gpt-a",
            ),
        )
    ]


_ROLE_NAMES = st.lists(
    st.from_regex(r"[a-z0-9][a-z0-9._-]{0,20}", fullmatch=True), max_size=6, unique=True
).map(tuple)


@given(roles=_ROLE_NAMES, asked=st.sampled_from([None, True]))
def test_the_reply_names_the_orchestrations_roles_exactly_when_they_were_asked_for(
    tmp_path_factory: pytest.TempPathFactory, roles: tuple[str, ...], *, asked: bool | None
) -> None:
    """The roles are the run's own report; a client that did not ask never sees the field."""
    report = PreflightReport(
        settings=SettingsValid(),
        uses_docker=False,
        agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.OK, detail="cached"),
        evaluator=Check(id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail="benchmark"),
        roles=roles,
    )
    project = _project(tmp_path_factory.mktemp("proj"))

    reply, _ = _preflight(project, report=report, with_roles=asked)

    assert isinstance(reply, RunPreflightReply)
    assert reply.roles == (roles if asked else None)
    assert ('"roles"' in reply.model_dump_json()) is bool(asked)


def test_asking_for_roles_needs_the_run_roles_feature() -> None:
    request = RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": "/p", "with_roles": True}
    )

    assert request_features(request) == (HelperFeature.PREFLIGHT, HelperFeature.RUN_ROLES)


def test_preflight_refuses_args_that_repeat_a_typed_option_naming_it(tmp_path: Path) -> None:
    reply, preflight = _preflight(
        _project(tmp_path / "proj"), args=["--max-r=4"], options={"rounds": 3}
    )

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.INVALID_REQUEST
    assert "--max-r=4" in reply.message
    assert preflight.calls == []


def test_a_role_model_the_harness_does_not_vet_is_refused(tmp_path: Path) -> None:
    reply, _ = _preflight(
        _project(tmp_path / "proj"),
        harness={"provider": "codex", "model": "gpt-a"},
        options={"roles": [{"role": "implementer", "model": "claude-a"}]},
    )

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNVETTED_HARNESS
    assert "claude-a" in reply.message


def test_run_start_refuses_an_unvetted_role_model_before_launching(tmp_path: Path) -> None:
    threads = SimThreads()
    launcher = FakeRunLauncher(failed_launch)
    host = fake_host(threads, SimNetwork(threads), launcher=launcher)
    request = RunStartRequest(
        request_id="s",
        deadline_ms=5000,
        project=str(_project(tmp_path / "proj")),
        harness=HarnessChoice(provider="codex", model="gpt-a"),
        options=RunOptions(roles=(RoleOption(role="implementer", model="nope"),)),
    )

    reply = answer(request, host, threads.event())

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNVETTED_HARNESS
    assert launcher.calls == []


_VALID = PreflightReport(
    settings=SettingsValid(),
    uses_docker=True,
    agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.WARN, detail="will be built"),
    evaluator=Check(id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail="benchmark"),
)


def _facts(repository: Path | None, docker: DockerReachable | DockerUnreachable) -> PreflightFacts:
    return PreflightFacts(
        project_root=Path("/work/repo/proj"),
        repository_root=repository,
        checkout=None,
        vibesys_version="0.1.1",
        docker=docker,
    )


@pytest.mark.parametrize(
    ("repository", "state", "fix"),
    [
        (Path("/work/repo/proj"), CheckState.OK, None),
        (None, CheckState.OK, None),
        (Path("/work/repo"), CheckState.FAIL, CopyOut(suggested_path="/work/proj")),
    ],
    ids=["own-repository", "no-repository", "inside-another"],
)
def test_a_project_inside_another_repository_fails_with_a_copy_out(
    repository: Path | None, state: CheckState, fix: CopyOut | None
) -> None:
    checks = preflight_checks(_facts(repository, DockerReachable(version="27")), _VALID)

    (check,) = [check for check in checks if check.id is CheckId.PROJECT_OWN_REPOSITORY]
    assert (check.state, check.fix) == (state, fix)


def test_an_unreachable_docker_fails_with_start_docker() -> None:
    checks = preflight_checks(_facts(None, DockerUnreachable(reason="daemon down")), _VALID)

    (check,) = [check for check in checks if check.id is CheckId.DOCKER_REACHABLE]
    assert (check.state, check.detail, check.fix) == (CheckState.FAIL, "daemon down", StartDocker())


def test_invalid_settings_fail_with_the_first_line_of_the_message() -> None:
    report = _VALID.model_copy(
        update={
            "settings": SettingsInvalid(
                code="config_load_failed",
                stage="config_loading",
                message="agent.roles.x: bad\nmore",
            )
        }
    )

    checks = preflight_checks(_facts(None, DockerReachable(version="27")), report)

    (check,) = [check for check in checks if check.id is CheckId.SETTINGS_VALID]
    assert (check.state, check.detail) == (CheckState.FAIL, "agent.roles.x: bad")


def test_start_is_enabled_exactly_when_no_check_fails() -> None:
    passing = preflight_checks(_facts(None, DockerReachable(version="27")), _VALID)
    failing = preflight_checks(_facts(None, DockerUnreachable(reason="down")), _VALID)

    assert all(check.state is not CheckState.FAIL for check in passing)
    assert any(check.state is CheckState.FAIL for check in failing)


def test_the_repository_root_comes_from_git(tmp_path: Path) -> None:
    machine = quiet_machine(
        lambda argv: (
            ProbeResult(0, f"{tmp_path}\n")
            if argv == ("git", "-C", str(tmp_path / "proj"), "rev-parse", "--show-toplevel")
            else None
        )
    )
    assert machine.repository_root(tmp_path / "proj") == tmp_path.resolve()
    assert machine.repository_root(tmp_path / "other") is None
    assert DOCKER_VERSION_COMMAND[0] == "docker"


def test_a_run_that_needs_no_docker_is_not_blocked_by_an_unreachable_docker() -> None:
    """Regression: every run failed ``docker_reachable`` without Docker, even one needing none."""
    report = _VALID.model_copy(update={"uses_docker": False})

    checks = preflight_checks(_facts(None, DockerUnreachable(reason="daemon down")), report)

    assert all(check.state is not CheckState.FAIL for check in checks)
    (docker,) = [check for check in checks if check.id is CheckId.DOCKER_REACHABLE]
    assert (docker.state, docker.fix) == (CheckState.OK, None)
    assert docker.detail.startswith("not needed")


@given(
    uses_docker=st.sampled_from([True, False, None]),
    docker=st.sampled_from([DockerReachable(version="27"), DockerUnreachable(reason="down")]),
)
def test_docker_fails_exactly_when_the_run_may_need_it_and_it_is_unreachable(
    *, uses_docker: bool | None, docker: DockerReachable | DockerUnreachable
) -> None:
    report = _VALID.model_copy(update={"uses_docker": uses_docker})

    checks = preflight_checks(_facts(None, docker), report)

    (check,) = [check for check in checks if check.id is CheckId.DOCKER_REACHABLE]
    unreachable = isinstance(docker, DockerUnreachable)
    assert (check.state is CheckState.FAIL) == (uses_docker is not False and unreachable)
    assert [each.id for each in checks] == _UNASKED_CHECKS


_UNAVAILABLE = BackendUnavailable(task="mpmc", backend=Backend.CUDA, declared=(Backend.CUDA,))


def _failing_backend_report() -> PreflightReport:
    return PreflightReport(
        settings=SettingsInvalid(
            code="accelerator_unavailable",
            stage="compute_backend",
            message="CLI wording: --backend may only name a backend the task declares",
            cause=_UNAVAILABLE,
        ),
        uses_docker=True,
        agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.WARN, detail="not checked"),
        evaluator=Check(
            id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.WARN, detail="not checked"
        ),
        backend=Backend.CUDA,
    )


@pytest.mark.parametrize("asked", [None, True])
def test_a_backend_failure_carries_its_typed_cause_and_the_backend_only_when_asked_for(
    tmp_path: Path, *, asked: bool | None
) -> None:
    reply, _ = _preflight(
        _project(tmp_path / "proj"), report=_failing_backend_report(), with_backend=asked
    )

    assert isinstance(reply, RunPreflightReply)
    settings = next(check for check in reply.checks if check.id is CheckId.SETTINGS_VALID)
    assert settings.state is CheckState.FAIL
    assert settings.cause == (_UNAVAILABLE if asked else None)
    assert reply.backend == (Backend.CUDA if asked else None)
    assert ('"cause"' in reply.model_dump_json()) is bool(asked)
    assert ('"backend"' in reply.model_dump_json()) is bool(asked)


def test_a_backend_failures_detail_names_the_task_and_never_a_flag_or_setting(
    tmp_path: Path,
) -> None:
    reply, _ = _preflight(_project(tmp_path / "proj"), report=_failing_backend_report())

    assert isinstance(reply, RunPreflightReply)
    settings = next(check for check in reply.checks if check.id is CheckId.SETTINGS_VALID)
    assert "mpmc" in settings.detail
    assert "--" not in settings.detail


def test_asking_for_the_backend_needs_the_run_backend_feature() -> None:
    request = RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": "/p", "with_backend": True}
    )

    assert request_features(request) == (HelperFeature.PREFLIGHT, HelperFeature.RUN_BACKEND)


def _report_with_changes(changes: tuple[str, ...]) -> PreflightReport:
    return _failing_backend_report().model_copy(
        update={"settings": SettingsValid(), "project_changes": changes}
    )


@given(changes=st.lists(st.from_regex(r"[a-z][a-z0-9_./]{0,20}", fullmatch=True), max_size=5))
def test_a_dirty_project_fails_project_clean_naming_the_project_and_every_path(
    tmp_path_factory: pytest.TempPathFactory, changes: list[str]
) -> None:
    """Regression: Ready to start was green and Start failed on ``must be clean``."""
    project = _project(tmp_path_factory.mktemp("proj"))

    reply, _ = _preflight(
        project, report=_report_with_changes(tuple(changes)), with_project_clean=True
    )

    assert isinstance(reply, RunPreflightReply)
    clean = next(check for check in reply.checks if check.id is CheckId.PROJECT_CLEAN)
    if changes:
        assert clean.state is CheckState.FAIL
        assert clean.cause == ProjectDirty(project=str(project.resolve()), paths=tuple(changes))
    else:
        assert (clean.state, clean.cause) == (CheckState.OK, None)


def test_the_clean_check_is_listed_in_check_order_only_when_asked_for(tmp_path: Path) -> None:
    project = _project(tmp_path / "proj")

    asked, _ = _preflight(project, with_project_clean=True)
    unasked, _ = _preflight(project)

    assert isinstance(asked, RunPreflightReply)
    assert isinstance(unasked, RunPreflightReply)
    assert [check.id for check in asked.checks] == _UNASKED_CHECKS
    assert CheckId.PROJECT_CLEAN not in [check.id for check in unasked.checks]


def test_asking_for_the_clean_check_needs_the_project_clean_feature() -> None:
    request = RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": "/p", "with_project_clean": True}
    )

    assert request_features(request) == (HelperFeature.PREFLIGHT, HelperFeature.PROJECT_CLEAN)
