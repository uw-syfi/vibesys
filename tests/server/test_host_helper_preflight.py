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
    DockerReachable,
    DockerUnreachable,
    HarnessChoice,
    HelperError,
    HelperErrorCode,
    RunPreflightReply,
    RunPreflightRequest,
    RunStartRequest,
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
from server.host_preflight import (
    Check,
    CheckId,
    CheckState,
    CopyOut,
    PreflightReport,
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


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


def _preflight(project: Path, **fields: object) -> tuple[object, FakeRunPreflight]:
    threads = SimThreads()
    preflight = FakeRunPreflight()
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
    assert [check.id for check in reply.checks] == list(CheckId)
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
    assert [each.id for each in checks] == list(CheckId)
