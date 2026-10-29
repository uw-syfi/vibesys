"""Login status uses the generated read contract and preserves provider facts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.server.host_helper_support import FakeAgentLogins, fake_host

from server.host_agent_logins import AgentLogin
from server.host_helper_requests import registry_read_source
from server.host_operation_reads import answer_registered_read
from vs_proto.api import (
    Oneof,
    ReadRequest,
    ReadResponse,
    ValidationError,
    from_mapping,
    to_mapping,
    validate,
)
from vs_proto.api import host_payloads as pb
from vs_sim.api.testing import SimNetwork, SimThreads


@pytest.mark.parametrize("source", [None, "account", "variable", "file"])
def test_native_login_status_preserves_provider_facts(source: str | None) -> None:
    how = None
    if source == "account":
        how = pb.AgentLoginHow(value=Oneof("account", pb.LoginAccount(kind="account")))
    elif source in {"variable", "file"}:
        how = pb.AgentLoginHow(
            value=Oneof("api_key", pb.LoginApiKey(kind="api_key", **{source: "AGENT_API_KEY"}))
        )
    facts = [
        pb.AgentLogin(
            provider="agent-a",
            provider_name="Agent A",
            installed=True,
            state=pb.AgentLoginState.SIGNED_IN,
            how=how,
            api_key_variables=["AGENT_API_KEY"],
            detail="Signed in",
            can_start_login=True,
        ),
        pb.AgentLogin(
            provider="agent-b", provider_name="Agent B", state=pb.AgentLoginState.UNKNOWN
        ),
    ]
    threads = SimThreads()
    logins = FakeAgentLogins(tuple(AgentLogin.model_validate(to_mapping(row)) for row in facts))
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        logins=logins,
    )
    payload = ReadRequest(
        checkout="/checkout",
        method=Oneof("list_agent_logins", pb.ListAgentLoginsRequest(request_id="login-status")),
    )
    response = ReadResponse.from_binary(
        answer_registered_read("list-agent-logins", payload.to_binary(), registry_read_source(host))
    )
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_agent_logins"
    assert response.result.value.request_id == "login-status"
    assert [to_mapping(row) for row in response.result.value.logins] == [
        to_mapping(row) for row in facts
    ]
    assert logins.started == []


@given(
    installed=st.booleans(),
    state=st.sampled_from(pb.AgentLoginState),
    how=st.sampled_from([None, "account", "variable", "file", "both"]),
)
def test_login_contract_requires_consistent_facts(
    *, installed: bool, state: pb.AgentLoginState, how: str | None
) -> None:
    credential = None
    if how == "account":
        credential = pb.AgentLoginHow(value=Oneof("account", pb.LoginAccount(kind="account")))
    elif how is not None:
        credential = pb.AgentLoginHow(
            value=Oneof(
                "api_key",
                pb.LoginApiKey(
                    kind="api_key",
                    variable="AGENT_API_KEY" if how in {"variable", "both"} else None,
                    file="credentials" if how in {"file", "both"} else None,
                ),
            )
        )
    login = pb.AgentLogin(
        provider="agent-a",
        provider_name="Agent A",
        installed=installed,
        state=state,
        how=credential,
    )
    valid = (
        state
        in {pb.AgentLoginState.SIGNED_IN, pb.AgentLoginState.SIGNED_OUT, pb.AgentLoginState.UNKNOWN}
        and (installed or state == pb.AgentLoginState.UNKNOWN)
        and (how is None or state == pb.AgentLoginState.SIGNED_IN)
        and how != "both"
    )
    if valid:
        validate(login)
    else:
        with pytest.raises(ValidationError):
            validate(login)


def test_login_status_rejects_unknown_metadata_keys() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        from_mapping(pb.AgentLogin, {"unexpected": "metadata"})


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[2] / "proto/testdata/operations/login-status.json"
        ).read_text()
    ),
    ids=lambda case: case["name"],
)
def test_login_status_cross_language_corpus(case: dict[str, object]) -> None:
    encoded = case["protobufHex"]
    assert isinstance(encoded, str)
    value = pb.AgentLogin.from_binary(bytes.fromhex(encoded))
    if case["valid"]:
        validate(value)
    else:
        with pytest.raises(ValidationError):
            validate(value)
