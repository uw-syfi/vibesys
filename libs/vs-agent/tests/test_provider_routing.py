"""A run's roles on several providers share one durable session journal through the router.

Each provider has its own client; ``ProviderRoutedExecutor`` sends a keyed turn to the client of
its session spec's provider, so a turn never reaches a client bound to another provider, and the
journal above it (``ClientAgentSessions``) works as it does over one client.
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_agent.api import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnExecutor,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    ProviderRoutedExecutor,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_prompts.api import TemplateRenderer
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

_PROVIDERS = ("alpha", "beta", "gamma")


class Reply(BaseModel):
    value: int


def _client(provider: str, **capabilities: bool) -> FakeAgentClient:
    client = FakeAgentClient(
        provider=provider,
        capabilities=AgentCapabilities(provider_session_resume=True, **capabilities),
        threads=SimThreads(),
    )
    client.set_response("worker", {"value": 1})
    return client


def _spec(provider: str, workspace: Path) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="worker",
        provider=provider,
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


def _turn(label: str) -> AgentTurnRequest:
    return AgentTurnRequest(message="work", output_schema=Reply, invocation_id=label)


@given(providers=st.lists(st.sampled_from(_PROVIDERS), min_size=1, max_size=8))
def test_a_turn_reaches_exactly_the_client_of_its_providers(
    tmp_path_factory: pytest.TempPathFactory, providers: list[str]
) -> None:
    workspace = tmp_path_factory.mktemp("workspace")
    clients = {provider: _client(provider) for provider in _PROVIDERS}
    router = ProviderRoutedExecutor(clients)
    assert isinstance(router, AgentTurnExecutor)

    for index, provider in enumerate(providers):
        key = AgentSessionKey(SessionScope.MEMBER, f"worker:{index}")
        router.run(
            session_spec=_spec(provider, workspace), turn=_turn(f"t{index}"), session_key=key
        )

    for provider, client in clients.items():
        assert len(client.calls) == providers.count(provider)


def test_a_provider_no_client_serves_is_refused_naming_it_and_runs_nothing(tmp_path: Path) -> None:
    clients = {"alpha": _client("alpha")}
    router = ProviderRoutedExecutor(clients)

    with pytest.raises(ValueError, match="'beta'"):
        router.run(session_spec=_spec("beta", tmp_path), turn=_turn("t"))

    assert clients["alpha"].calls == []


def test_a_router_needs_a_client() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ProviderRoutedExecutor({})


@given(
    flags=st.lists(
        st.fixed_dictionaries({field.name: st.booleans() for field in fields(AgentCapabilities)}),
        min_size=1,
        max_size=3,
    )
)
def test_a_guarantee_holds_only_when_every_client_gives_it(flags: list[dict[str, bool]]) -> None:
    executors = {
        provider: FakeAgentClient(
            provider=provider, capabilities=AgentCapabilities(**flag), threads=SimThreads()
        )
        for provider, flag in zip(_PROVIDERS, flags, strict=False)
    }

    reported = ProviderRoutedExecutor(executors).capabilities

    for field in fields(AgentCapabilities):
        assert getattr(reported, field.name) == all(flag[field.name] for flag in flags)


@given(providers=st.lists(st.sampled_from(_PROVIDERS), min_size=1, max_size=5, unique=True))
def test_the_journal_continues_each_conversation_on_the_client_that_started_it(
    tmp_path_factory: pytest.TempPathFactory, providers: list[str]
) -> None:
    workspace = tmp_path_factory.mktemp("workspace")
    clients = {provider: _client(provider, session_reuse=True) for provider in providers}
    sessions = ClientAgentSessions(
        ProviderRoutedExecutor(clients), FakeAgentInvocationStore(), threads=SimThreads()
    )
    keys = {p: AgentSessionKey(SessionScope.MEMBER, f"worker:{p}") for p in providers}

    for provider in providers:
        started = sessions.start(keys[provider], _spec(provider, workspace), _turn(f"s-{provider}"))
        assert isinstance(started, Completed)
    message = TemplateRenderer(workspace).render_string("trusted result")
    for provider in providers:
        initial = _turn(f"s-{provider}")
        sessions.bind(
            keys[provider],
            _spec(provider, workspace),
            replace(initial, message="", invocation_id=None),
        )
        resumed = sessions.resume(keys[provider], message, f"r-{provider}")
        assert isinstance(resumed, Completed)

    for provider, client in clients.items():
        # Its own conversation's start and resume, and nothing of another provider's.
        assert len(client.calls) == 2
        assert {call.session_key for call in client.calls} == {keys[provider]}


def test_a_conversation_is_cancelled_and_released_on_the_client_that_ran_it(
    tmp_path: Path,
) -> None:
    clients = {provider: _client(provider) for provider in ("alpha", "beta")}
    router = ProviderRoutedExecutor(clients)
    key = AgentSessionKey(SessionScope.MEMBER, "worker:one")
    other = AgentSessionKey(SessionScope.MEMBER, "worker:never-ran")
    router.run(session_spec=_spec("beta", tmp_path), turn=_turn("t"), session_key=key)

    router.cancel_session(key)
    router.release_session(key)
    router.cancel_session(other)
    router.release_session(other)

    assert clients["beta"].cancelled_sessions == [key]
    assert clients["beta"].released_sessions == [key]
    assert clients["alpha"].cancelled_sessions == []
    assert clients["alpha"].released_sessions == []
