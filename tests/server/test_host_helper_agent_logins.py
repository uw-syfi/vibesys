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

from entrypoints.host_helper import AgentLibraryLogins
from server.host_agent_logins import (
    AccountLogin,
    AgentLogin,
    AgentLoginsListReply,
    AgentLoginsListRequest,
    AgentLoginsStartReply,
    AgentLoginsStartRequest,
    ApiKeyLogin,
    LoginStarted,
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
from vs_agent.api import LoginCredential, LoginKind, ProviderLogin
from vs_agent.api import LoginState as ProviderLoginState
from vs_decode.api import Unreadable
from vs_sim.api.testing import SimNetwork, SimThreads

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


def _adapter(**by_provider: ProviderLogin) -> AgentLibraryLogins:
    """Providers not given are absent from the machine."""
    return AgentLibraryLogins(
        lambda provider: by_provider.get(
            provider, _found(provider, installed=False, state=ProviderLoginState.UNKNOWN)
        )
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
    assert all(not login.can_start_login for login in listed.values())
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


@pytest.mark.parametrize(
    ("installed", "reason"),
    [
        (True, LoginUnavailableReason.NO_LOGIN_COMMAND),
        (False, LoginUnavailableReason.NOT_INSTALLED),
    ],
)
def test_agentshim_names_no_login_command_so_starting_one_says_why_not(
    *, installed: bool, reason: LoginUnavailableReason
) -> None:
    adapter = _adapter(
        claude=_found("claude", installed=installed, state=ProviderLoginState.UNKNOWN)
    )

    assert adapter.start("claude") == LoginUnavailable(reason=reason)
