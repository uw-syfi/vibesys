"""The run options the desktop app sets reach the run's configuration, and ``vibesys preflight``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints import cli
from entrypoints.cli.config import load_config_and_skills
from entrypoints.cli.preflight import AGENT_IMAGES_COMMAND, agent_image_check, preflight_report
from server.host_api import MAX_RUN_SECONDS_FLAG, ROLE_MODEL_FLAG, ROLE_REASONING_EFFORT_FLAG
from server.host_preflight import CheckId, CheckState, SettingsInvalid
from vibesys.api import DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from argparse import Namespace
    from pathlib import Path


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--outer-loop",
        "agent",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--input-objective",
        "Serve fast.",
        "--input-domain",
        "llm-serving",
        "--input-accuracy-command",
        "python checker.py",
        "--input-benchmark-command",
        "python benchmark.py",
        "--no-skills",
        *extra,
    ]


_ROLE = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)


@given(
    seconds=st.integers(1, 10**6),
    models=st.dictionaries(_ROLE, st.from_regex(r"[a-z0-9.-]{1,12}", fullmatch=True), max_size=3),
    efforts=st.dictionaries(_ROLE, st.sampled_from(["low", "medium", "high"]), max_size=3),
)
def test_time_limit_and_role_flags_override_the_configuration(
    tmp_path_factory: pytest.TempPathFactory,
    seconds: int,
    models: dict[str, str],
    efforts: dict[str, str],
) -> None:
    tmp_path = tmp_path_factory.mktemp("run")
    flags = [MAX_RUN_SECONDS_FLAG, str(seconds)]
    for role, model in models.items():
        flags += [ROLE_MODEL_FLAG, f"{role}={model}"]
    for role, effort in efforts.items():
        flags += [ROLE_REASONING_EFFORT_FLAG, f"{role}={effort}"]
    args = cli.parse_cli_invocation(_argv(tmp_path, *flags)).args

    config, _skills, _backend = load_config_and_skills(args, domain=DomainName.LLM_SERVING)

    assert config.run.max_run_seconds == seconds
    assert {role: cfg.model for role, cfg in config.agent.roles.items() if cfg.model} == models
    assert {
        role: cfg.reasoning_effort
        for role, cfg in config.agent.roles.items()
        if cfg.reasoning_effort
    } == efforts


@pytest.mark.parametrize(
    "flags",
    [
        [MAX_RUN_SECONDS_FLAG, "0"],
        [MAX_RUN_SECONDS_FLAG, "soon"],
        [ROLE_MODEL_FLAG, "implementer"],
        [ROLE_REASONING_EFFORT_FLAG, "=high"],
    ],
)
def test_a_malformed_option_is_a_configuration_error(tmp_path: Path, flags: list[str]) -> None:
    with pytest.raises(ConfigurationError):
        cli.parse_cli_invocation(_argv(tmp_path, *flags))


def test_a_role_name_the_configuration_cannot_hold_is_refused_naming_the_key(
    tmp_path: Path,
) -> None:
    args = cli.parse_cli_invocation(_argv(tmp_path, ROLE_MODEL_FLAG, "Bad Role=x")).args

    with pytest.raises(ConfigurationError, match=r"agent\.roles"):
        load_config_and_skills(args, domain=DomainName.LLM_SERVING)


def test_an_invalid_configuration_is_reported_and_nothing_else_is_checked() -> None:
    diagnostic = ConfigurationDiagnostic(
        code="agent_role_configuration_invalid",
        stage="agent_configuration_validation",
        message="agent configuration names roles not declared: agent.roles.bogus",
    )

    def parse(_argv: list[str]) -> Namespace:
        raise ConfigurationError(diagnostic)

    probe = ScriptedProbe(lambda _argv: None)
    report = preflight_report(["--role-model", "bogus=x"], parse, probe)

    assert report.settings == SettingsInvalid(
        code=diagnostic.code, stage=diagnostic.stage, message=diagnostic.message
    )
    assert report.agent_image.state is CheckState.WARN
    assert report.evaluator.state is CheckState.WARN
    assert probe.calls == []


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        (ProbeResult(0, "sha256:abc\n"), CheckState.OK),
        (ProbeResult(0, ""), CheckState.WARN),
        (ProbeResult(1, "", "Cannot connect"), CheckState.WARN),
        (None, CheckState.WARN),
    ],
    ids=["cached", "never-built", "docker-down", "docker-missing"],
)
def test_the_agent_image_is_ok_once_built_here_and_a_warning_before(
    answer: ProbeResult | None, state: CheckState
) -> None:
    check = agent_image_check(
        ScriptedProbe(lambda argv: answer if argv == AGENT_IMAGES_COMMAND else None)
    )

    assert check.id is CheckId.AGENT_IMAGE
    assert check.state is state
