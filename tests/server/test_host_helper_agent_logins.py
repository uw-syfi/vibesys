"""``agent_logins.list`` and ``agent_logins.start``: each harness's login on the machine.

The requests run through :func:`answer` over a Fake of the host's ``AgentLogins``; the adapter
that maps the agent library's facts onto the wire runs over a scripted ``ProviderLogin``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import FakeAgentLogins, fake_host

from server.host_agent_logins import (
    AccountLogin,
    AgentLibraryLogins,
    AgentLogin,
    AgentLoginsListReply,
    AgentLoginsListRequest,
    AgentLoginsStartReply,
    AgentLoginsStartRequest,
    ApiKeyLogin,
    LoginBounds,
    LoginStarted,
    LoginStarter,
    LoginState,
    LoginUnavailable,
    LoginUnavailableReason,
)
from server.host_api import (
    HelperError,
    HelperErrorCode,
    HelperFeature,
    parse_helper_message,
    parse_helper_request,
    request_features,
)
from server.host_helper_requests import answer
from vs_agent.api import LoginCommand, LoginCredential, LoginKind, ProviderLogin
from vs_agent.api import LoginState as ProviderLoginState
from vs_decode.api import Unreadable
from vs_sim.api.testing import (
    HANG_GUARD_S,
    FakePipedProcessLauncher,
    ScriptedPipedProcess,
    SimNetwork,
    SimThreads,
)

_NAME = st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True)
_VARIABLE = st.from_regex(r"[A-Z][A-Z0-9_]{0,20}", fullmatch=True)


def _logins() -> st.SearchStrategy[AgentLogin]:
    installed = st.builds(
        AgentLogin,
        provider=_NAME,
        provider_name=_NAME,
        installed=st.sampled_from([True]),
        state=st.sampled_from(LoginState),
        api_key_variables=st.lists(_VARIABLE, max_size=3, unique=True).map(tuple),
        can_start_login=st.booleans(),
    )
    missing = st.builds(
        AgentLogin,
        provider=_NAME,
        provider_name=_NAME,
        installed=st.sampled_from([False]),
        state=st.sampled_from([LoginState.UNKNOWN]),
        can_start_login=st.sampled_from([False]),
    )
    return installed | missing


def _ask(
    request: AgentLoginsListRequest | AgentLoginsStartRequest, logins: FakeAgentLogins
) -> object:
    threads = SimThreads()

    host = replace(fake_host(threads, SimNetwork(threads)), logins=logins)
    return answer(request, host, threads.event())


@given(logins=st.lists(_logins(), max_size=4, unique_by=lambda login: login.provider))
def test_the_list_is_the_hosts_logins_in_its_order(logins: list[AgentLogin]) -> None:
    reply = _ask(
        AgentLoginsListRequest(request_id="r", deadline_ms=5000), FakeAgentLogins(tuple(logins))
    )

    assert isinstance(reply, AgentLoginsListReply)
    assert reply.logins == tuple(logins)
    line = reply.model_dump_json().encode()
    assert parse_helper_message(line) == reply


@given(logins=st.lists(_logins(), min_size=1, max_size=3, unique_by=lambda login: login.provider))
def test_starting_a_login_asks_the_host_for_a_provider_it_lists(logins: list[AgentLogin]) -> None:
    fake = FakeAgentLogins(tuple(logins), outcome=LoginStarted(instruction="open the link"))
    chosen = logins[0].provider

    reply = _ask(AgentLoginsStartRequest(request_id="r", deadline_ms=5000, provider=chosen), fake)

    assert isinstance(reply, AgentLoginsStartReply)
    assert reply.result == LoginStarted(instruction="open the link")
    assert fake.started == [chosen]


@given(provider=_NAME)
def test_a_provider_the_host_does_not_offer_is_refused_and_starts_nothing(provider: str) -> None:
    fake = FakeAgentLogins()

    reply = _ask(AgentLoginsStartRequest(request_id="r", deadline_ms=5000, provider=provider), fake)

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.UNKNOWN_PROVIDER
    assert provider in reply.message
    assert fake.started == []


def test_both_requests_need_the_agent_logins_feature() -> None:
    base = {"request_id": "r", "deadline_ms": 5000}
    for line in (
        {**base, "type": "agent_logins.list"},
        {**base, "type": "agent_logins.start", "provider": "claude"},
    ):
        request = parse_helper_request(json.dumps(line).encode())
        assert not isinstance(request, Unreadable)
        assert request_features(request) == (HelperFeature.AGENT_LOGINS,)


@given(extra=_NAME)
def test_an_unknown_key_is_rejected(extra: str) -> None:
    with pytest.raises(ValidationError):
        AgentLogin.model_validate(
            {
                "provider": "x",
                "provider_name": "X",
                "installed": True,
                "state": "signed_in",
                "can_start_login": False,
                extra: 1,
            }
        )


def test_the_facts_of_one_login_must_agree() -> None:
    base = {"provider": "x", "provider_name": "X", "can_start_login": False}
    unreachable = {**base, "installed": False, "state": "signed_in"}
    signed_out = {**base, "installed": True, "state": "signed_out", "how": {"kind": "account"}}
    with pytest.raises(ValidationError):
        AgentLogin.model_validate(unreachable)
    with pytest.raises(ValidationError):
        AgentLogin.model_validate(signed_out)
    with pytest.raises(ValidationError):
        ApiKeyLogin(variable="A", file=".x")


# --- the adapter over the agent library's facts ---------------------------------------------


def _found(
    provider: str,
    *,
    installed: bool = True,
    state: ProviderLoginState = ProviderLoginState.SIGNED_IN,
    credential: LoginCredential | None = None,
) -> ProviderLogin:
    return ProviderLogin(
        provider=provider,
        installed=installed,
        state=state,
        credential=credential,
        api_key_variables=("SOME_API_KEY",),
        detail="Logged in",
    )


_COMMANDS = {
    "claude": LoginCommand(("claude", "auth", "login"), needs_terminal=True),
    "codex": LoginCommand(("codex", "login", "--device-auth"), needs_terminal=False),
}
"""The commands the agent library names in these tests: one needs a terminal, one does not."""


def _adapter(
    launcher: FakePipedProcessLauncher | None = None,
    threads: SimThreads | None = None,
    commands: dict[str, LoginCommand] | None = None,
    **by_provider: ProviderLogin,
) -> AgentLibraryLogins:
    """Providers not given are absent from the machine; a launcher not given refuses any start."""
    threads = threads or SimThreads()
    return AgentLibraryLogins(
        lambda provider: by_provider.get(
            provider, _found(provider, installed=False, state=ProviderLoginState.UNKNOWN)
        ),
        (_COMMANDS if commands is None else commands).get,
        LoginStarter(launcher or FakePipedProcessLauncher(), threads, dict, LoginBounds()),
    )


def _listed(adapter: AgentLibraryLogins) -> dict[str, AgentLogin]:
    return {login.provider: login for login in adapter.list()}


def test_a_signed_in_account_and_an_api_key_are_told_apart_without_the_secret() -> None:
    account = _found("claude", credential=LoginCredential(LoginKind.ACCOUNT))
    keyed = _found("codex", credential=LoginCredential(LoginKind.API_KEY, variable="CODEX_API_KEY"))
    listed = _listed(_adapter(claude=account, codex=keyed))
    filed = _found("codex", credential=LoginCredential(LoginKind.API_KEY, file=".codex/k.json"))

    assert listed["claude"].how == AccountLogin()
    assert listed["codex"].how == ApiKeyLogin(variable="CODEX_API_KEY")
    assert _listed(_adapter(codex=filed))["codex"].how == ApiKeyLogin(file=".codex/k.json")
    assert all(login.api_key_variables == ("SOME_API_KEY",) for login in listed.values())


@pytest.mark.parametrize("state", list(ProviderLoginState))
def test_a_login_state_is_carried_over_and_a_missing_cli_is_unknown(
    state: ProviderLoginState,
) -> None:
    listed = _listed(_adapter(claude=_found("claude", state=state)))

    assert listed["claude"].state.value == state.value
    missing = [login for login in listed.values() if login.provider != "claude"]
    assert missing
    assert all(not login.installed and login.state is LoginState.UNKNOWN for login in missing)


def test_a_login_can_be_started_only_for_an_installed_cli_whose_command_needs_no_terminal() -> None:
    listed = _listed(_adapter(claude=_found("claude"), codex=_found("codex")))
    uninstalled = _listed(_adapter())
    commandless = _listed(_adapter(commands={}, codex=_found("codex")))

    assert not listed["claude"].can_start_login  # prompts the person
    assert listed["codex"].can_start_login
    assert not commandless["codex"].can_start_login  # agentshim names no command for it
    assert all(not login.can_start_login for login in uninstalled.values())


def test_a_missing_cli_is_reported_before_anything_else_and_starts_nothing() -> None:
    launcher = FakePipedProcessLauncher()
    adapter = _adapter(
        launcher, codex=_found("codex", installed=False, state=ProviderLoginState.UNKNOWN)
    )

    assert adapter.start("codex") == LoginUnavailable(reason=LoginUnavailableReason.NOT_INSTALLED)
    assert launcher.started == []


def test_a_cli_with_no_login_command_says_so() -> None:
    launcher = FakePipedProcessLauncher()
    adapter = _adapter(launcher, commands={}, codex=_found("codex"))

    assert adapter.start("codex") == LoginUnavailable(
        reason=LoginUnavailableReason.NO_LOGIN_COMMAND
    )
    assert launcher.started == []


def test_a_login_that_prompts_is_named_for_a_terminal_and_not_run() -> None:
    launcher = FakePipedProcessLauncher()
    adapter = _adapter(launcher, claude=_found("claude", state=ProviderLoginState.SIGNED_OUT))

    assert adapter.start("claude") == LoginUnavailable(
        reason=LoginUnavailableReason.NEEDS_TERMINAL, command="claude auth login"
    )
    assert launcher.started == []


@given(url=st.from_regex(r"https://[a-z]{3,10}\.example/[a-z]{1,8}", fullmatch=True))
def test_a_login_that_needs_no_terminal_is_run_with_the_command_the_agent_library_names(
    url: str,
) -> None:
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout=f"Open {url}\nCode ABCD-EFGH1\n")
    launcher = FakePipedProcessLauncher(lambda _start: process)
    adapter = _adapter(
        launcher, threads, codex=_found("codex", state=ProviderLoginState.SIGNED_OUT)
    )

    result = threads.run(lambda: adapter.start("codex"))

    assert result == LoginStarted(instruction=f"Open {url} and enter the code ABCD-EFGH1")
    assert [started.argv for started in launcher.started] == [("codex", "login", "--device-auth")]


@pytest.mark.parametrize(
    ("installed", "commands", "reason"),
    [
        (False, _COMMANDS, LoginUnavailableReason.NOT_INSTALLED),
        (True, {}, LoginUnavailableReason.NO_LOGIN_COMMAND),
        (
            True,
            {"codex": LoginCommand(("codex", "login"), needs_terminal=True)},
            LoginUnavailableReason.NEEDS_TERMINAL,
        ),
    ],
)
def test_holding_a_refused_login_is_already_finished_and_launches_nothing(
    *, installed: bool, commands: dict[str, LoginCommand], reason: LoginUnavailableReason
) -> None:
    threads = SimThreads()
    launcher = FakePipedProcessLauncher()
    adapter = _adapter(
        launcher,
        threads,
        commands=commands,
        codex=_found("codex", installed=installed, state=ProviderLoginState.UNKNOWN),
    )

    def run() -> None:
        held = adapter.hold("codex")
        assert isinstance(held.result, LoginUnavailable)
        assert held.result.reason is reason
        if reason is LoginUnavailableReason.NEEDS_TERMINAL:
            assert held.result.command == "codex login"
        held.cancel()
        waiter = threads.spawn(held.wait, name="login-reaped")
        waiter.join(HANG_GUARD_S)
        assert not waiter.is_alive()

    threads.run(run)
    assert launcher.started == []


def test_holding_an_unoffered_provider_is_rejected_before_launch() -> None:
    launcher = FakePipedProcessLauncher()
    with pytest.raises(ValueError, match="offers no harness"):
        _adapter(launcher).hold("unoffered-provider")
    assert launcher.started == []


def test_a_login_whose_exit_cannot_be_watched_is_ended_instead_of_left_running() -> None:
    """A failed wait used to skip the lifetime's cleanup, so the CLI kept running past it."""
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True, wait_failure=OSError("no watch"))
    process.write(stdout="Open https://login.example/device\nCode ABCD-EFGH1\n")
    adapter = _adapter(
        FakePipedProcessLauncher(lambda _start: process), threads, codex=_found("codex")
    )

    def run() -> None:
        held = adapter.hold("codex")
        waiter = threads.spawn(held.wait, name="login-reaped")
        waiter.join(HANG_GUARD_S)
        assert not waiter.is_alive()
        assert process.signals == ["kill"]

    with pytest.raises(OSError, match="no watch"):  # The reaper thread lets the failure surface.
        threads.run(run)


def test_a_held_login_keeps_the_process_until_cancel_and_reaps_it() -> None:
    threads = SimThreads()
    process = ScriptedPipedProcess(threads, merge_stderr=True)
    process.write(stdout="Open https://login.example/device\nCode ABCD-EFGH1\n")
    launcher = FakePipedProcessLauncher(lambda _start: process)
    adapter = _adapter(launcher, threads, codex=_found("codex"))

    def run() -> None:
        held = adapter.hold("codex")
        assert held.result == LoginStarted(
            instruction="Open https://login.example/device and enter the code ABCD-EFGH1"
        )
        assert process.signals == []
        held.cancel()
        waiter = threads.spawn(held.wait, name="login-reaped")
        waiter.join(HANG_GUARD_S)
        assert not waiter.is_alive()
        assert process.signals == ["terminate"]

    threads.run(run)
    assert [started.argv for started in launcher.started] == [("codex", "login", "--device-auth")]
