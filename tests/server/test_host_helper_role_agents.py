"""``run.start`` and ``run.preflight``'s ``agents``: a vetted provider and model per role.

A role with an entry runs its own pair, a role without one runs the run's ``harness``, and a key
the run does not declare or a pair it does not offer is refused naming the key. The requests run
through :func:`answer` over the Fakes of ``host_helper_support``, with a Fake of the run's own
validation (``RolesAwarePreflight``) that refuses the roles its orchestration does not declare as
the run does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError
from tests.server.host_helper_support import (
    HARNESSES,
    FakeRunLauncher,
    fake_host,
    record,
)

from server.host_api import (
    MODEL_FLAG,
    ROLE_MODEL_FLAG,
    ROLE_PROVIDER_FLAG,
    HarnessChoice,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    RunPreflightReply,
    RunPreflightRequest,
    RunStartReply,
    RunStartRequest,
    request_features,
)
from server.host_helper_preflight import option_arguments, option_flags, repeated_option
from server.host_helper_requests import answer, launch_arguments, run_arguments
from server.host_preflight import (
    Check,
    CheckId,
    CheckState,
    PreflightReport,
    RoleName,
    RoleOption,
    RolesNotDeclared,
    RunOptions,
    SettingsInvalid,
    SettingsValid,
)
from vs_sim.api.testing import SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_ROLE_ID = TypeAdapter(RoleName)


def _is_role_id(text: str) -> bool:
    try:
        _ROLE_ID.validate_python(text)
    except ValidationError:
        return False
    return True


_DECLARED = ("planner", "implementer", "reviewer")
_UNDECLARED = ("ghost", "phantom")
_VETTED = tuple(HarnessChoice(provider=h.provider, model=h.model) for h in HARNESSES)
_UNVETTED = (
    HarnessChoice(provider="codex", model="gpt-z"),
    HarnessChoice(provider="claude", model="gpt-a"),
    HarnessChoice(provider="gemini", model="gemini-a"),
)


@dataclass
class RolesAwarePreflight:
    """The run's own validation, as far as roles go: it declares ``roles`` and refuses others.

    It reads the roles the helper wrote into the arguments (``--role-provider ROLE=...`` and
    ``--role-model ROLE=...``), as the run reads them from its command line.
    """

    roles: tuple[str, ...] = _DECLARED
    calls: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)

    def report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        self.calls.append((project, arguments))
        named = [
            arguments[index + 1].partition("=")[0]
            for index, token in enumerate(arguments)
            if token in {ROLE_PROVIDER_FLAG, ROLE_MODEL_FLAG}
        ]
        unknown = tuple(dict.fromkeys(role for role in named if role not in self.roles))
        evaluator = Check(id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail="benchmark")
        image = Check(id=CheckId.AGENT_IMAGE, state=CheckState.OK, detail="cached")
        if unknown:
            return PreflightReport(
                settings=SettingsInvalid(
                    code="agent_role_configuration_invalid",
                    stage="agent_configuration_validation",
                    message="agent configuration names roles not declared",
                    cause=RolesNotDeclared(roles=unknown),
                ),
                uses_docker=None,
                agent_image=image,
                evaluator=evaluator,
            )
        return PreflightReport(
            settings=SettingsValid(),
            uses_docker=False,
            agent_image=image,
            evaluator=evaluator,
            roles=self.roles,
        )


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


def _agents(
    roles: st.SearchStrategy[str], pairs: st.SearchStrategy[HarnessChoice]
) -> st.SearchStrategy[dict[str, HarnessChoice]]:
    return st.dictionaries(roles, pairs, min_size=1, max_size=3)


_VALID_AGENTS = _agents(st.sampled_from(_DECLARED), st.sampled_from(_VETTED))


def _answer(
    request: RunStartRequest | RunPreflightRequest, project: Path
) -> tuple[object, FakeRunLauncher, RolesAwarePreflight]:
    started = record("0123456789ab", project_root=str(project))
    launcher = FakeRunLauncher(lambda _project, _arguments: started)
    preflight = RolesAwarePreflight()
    threads = SimThreads()
    host = replace(fake_host(threads, SimNetwork(threads), launcher=launcher), preflight=preflight)
    return (
        answer(request.model_copy(update={"project": str(project)}), host, threads.event()),
        launcher,
        preflight,
    )


def _start(agents: Mapping[str, object] | None, **fields: object) -> RunStartRequest:
    return RunStartRequest.model_validate(
        {"request_id": "s", "deadline_ms": 5000, "project": "/p", "agents": agents, **fields}
    )


def _preflight_request(
    agents: Mapping[str, object] | None, **fields: object
) -> RunPreflightRequest:
    return RunPreflightRequest.model_validate(
        {"request_id": "p", "deadline_ms": 5000, "project": "/p", "agents": agents, **fields}
    )


def _pairs(arguments: tuple[str, ...], flag: str) -> dict[str, str]:
    values = [arguments[i + 1] for i, token in enumerate(arguments) if token == flag]
    return dict(value.split("=", 1) for value in values)


@given(agents=_VALID_AGENTS)
def test_each_role_agent_becomes_its_provider_and_model_flags_and_other_roles_get_none(
    agents: dict[str, HarnessChoice],
) -> None:
    arguments = option_arguments(None, agents)

    assert _pairs(arguments, ROLE_PROVIDER_FLAG) == {r: c.provider for r, c in agents.items()}
    assert _pairs(arguments, ROLE_MODEL_FLAG) == {r: c.model for r, c in agents.items()}
    # A role without an entry is named by no flag, so it runs the run's harness.
    assert (
        not {role for role in _DECLARED if role not in agents}
        & _pairs(arguments, ROLE_PROVIDER_FLAG).keys()
    )


@given(agents=_VALID_AGENTS, options=st.builds(RunOptions, rounds=st.none() | st.integers(1, 9)))
def test_agents_flags_never_displace_a_typed_options_flags(
    agents: dict[str, HarnessChoice], options: RunOptions
) -> None:
    without = option_arguments(options)
    both = option_arguments(options, agents)

    assert [token for token in both if token not in option_arguments(None, agents)] == list(without)


@given(agents=_VALID_AGENTS, flag=st.sampled_from([ROLE_PROVIDER_FLAG, ROLE_MODEL_FLAG]))
def test_args_may_not_repeat_the_flags_agents_set(
    agents: dict[str, HarnessChoice], flag: str
) -> None:
    assert set(option_flags(None, agents)) == {ROLE_PROVIDER_FLAG, ROLE_MODEL_FLAG}
    refusal = repeated_option((f"{flag}=x=y",), None, agents)
    assert refusal is not None
    assert flag in refusal
    assert repeated_option((f"{flag}=x=y",), None, None) is None


@given(agents=_VALID_AGENTS, harness=st.none() | st.sampled_from(_VETTED))
def test_run_start_launches_with_the_agents_after_the_harness(
    tmp_path_factory: pytest.TempPathFactory,
    agents: dict[str, HarnessChoice],
    harness: HarnessChoice | None,
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    request = _start(agents, harness=harness)

    reply, launcher, preflight = _answer(request, project)

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    assert launched == launch_arguments(request)
    # The harness is the run's own agent: it is named once, ahead of every role's.
    assert (MODEL_FLAG in launched) == (harness is not None)
    assert _pairs(launched, ROLE_PROVIDER_FLAG) == {r: c.provider for r, c in agents.items()}
    assert _pairs(launched, ROLE_MODEL_FLAG) == {r: c.model for r, c in agents.items()}
    # The run's own validation answered before anything launched.
    assert [arguments for _, arguments in preflight.calls] == [run_arguments(request)]


@given(
    agents=_agents(st.sampled_from(_DECLARED), st.sampled_from(_VETTED + _UNVETTED)),
)
def test_a_pair_that_is_not_vetted_is_refused_naming_the_role_before_anything_runs(
    tmp_path_factory: pytest.TempPathFactory, agents: dict[str, HarnessChoice]
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    unvetted = [role for role, choice in agents.items() if choice not in _VETTED]

    reply, launcher, preflight = _answer(_start(agents), project)
    checked, _, preflight_run = _answer(_preflight_request(agents), project)

    if not unvetted:
        assert isinstance(reply, RunStartReply)
        assert isinstance(checked, RunPreflightReply)
        return
    for outcome in (reply, checked):
        assert isinstance(outcome, HelperError)
        assert outcome.code is HelperErrorCode.UNVETTED_HARNESS
        assert f"agents.{unvetted[0]}" in outcome.message
    assert launcher.calls == []
    assert preflight.calls == []
    assert preflight_run.calls == []


@given(
    agents=_agents(st.sampled_from(_DECLARED + _UNDECLARED), st.sampled_from(_VETTED)),
)
def test_a_role_the_run_does_not_declare_is_refused_naming_the_key(
    tmp_path_factory: pytest.TempPathFactory, agents: dict[str, HarnessChoice]
) -> None:
    project = _project(tmp_path_factory.mktemp("proj"))
    unknown = [role for role in agents if role in _UNDECLARED]

    reply, launcher, _ = _answer(_start(agents), project)
    checked, _, _ = _answer(_preflight_request(agents), project)

    if not unknown:
        assert isinstance(reply, RunStartReply)
        assert isinstance(checked, RunPreflightReply)
        return
    for outcome in (reply, checked):
        assert isinstance(outcome, HelperError)
        assert outcome.code is HelperErrorCode.INVALID_REQUEST
        assert all(f"agents.{role}" in outcome.message for role in unknown)
        assert not any(
            f"agents.{role}" in outcome.message for role in agents if role not in unknown
        )
    assert launcher.calls == []


def test_the_typed_model_of_a_role_with_an_agent_is_refused_naming_both_keys(
    tmp_path: Path,
) -> None:
    agents = {"implementer": _VETTED[0]}
    options = RunOptions(roles=(RoleOption(role="implementer", model="gpt-a"),))

    reply, launcher, _ = _answer(
        _start(agents, options=options.model_dump(mode="json")), _project(tmp_path / "p")
    )

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.INVALID_REQUEST
    assert "agents.implementer" in reply.message
    assert "options.roles" in reply.message
    assert launcher.calls == []


def test_a_role_with_an_agent_may_still_set_its_reasoning_effort(tmp_path: Path) -> None:
    agents = {"implementer": _VETTED[0]}
    options = RunOptions(roles=(RoleOption(role="implementer", reasoning_effort="high"),))

    reply, launcher, _ = _answer(
        _start(agents, options=options.model_dump(mode="json")), _project(tmp_path / "p")
    )

    assert isinstance(reply, RunStartReply)
    assert launcher.calls != []


@given(role=st.text(max_size=70).filter(lambda text: not _is_role_id(text)))
def test_a_key_that_is_not_a_role_id_is_rejected_naming_it(role: str) -> None:
    with pytest.raises(ValidationError) as raised:
        RunStartRequest.model_validate(
            {
                "request_id": "s",
                "deadline_ms": 1,
                "project": "/p",
                "agents": {role: {"provider": "codex", "model": "gpt-a"}},
            }
        )
    assert f"agents.{role}" in str(raised.value)


def test_a_pair_with_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _start({"implementer": {"provider": "codex", "model": "gpt-a", "effort": "high"}})


def test_no_entries_ask_the_run_to_change_nothing(tmp_path: Path) -> None:
    request = _start({})

    reply, launcher, preflight = _answer(request, _project(tmp_path / "p"))

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    assert ROLE_PROVIDER_FLAG not in launched
    assert preflight.calls == []


def test_agents_need_the_run_role_agents_feature() -> None:
    entries = {"implementer": _VETTED[0]}

    assert HelperFeature.RUN_ROLE_AGENTS in request_features(_start(entries))
    assert HelperFeature.RUN_ROLE_AGENTS in request_features(_preflight_request(entries))
    assert HelperFeature.RUN_ROLE_AGENTS not in request_features(_start(None))
