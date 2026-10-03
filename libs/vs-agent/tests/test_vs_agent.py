"""Tests for the pure agent value types: session identity and selection."""

import pytest

from vs_agent.api import AgentSelection, AgentSessionKey, SessionScope


def test_session_key_serializes_to_the_stored_form() -> None:
    key = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")

    assert str(key) == "hypothesis:H-01"
    assert AgentSessionKey.parse("hypothesis:H-01") == key


def test_only_hypothesis_and_chat_conversations_are_durable() -> None:
    assert AgentSessionKey(SessionScope.HYPOTHESIS, "H-01").durable
    assert AgentSessionKey(SessionScope.CHAT, "thread-a").durable
    assert not AgentSessionKey(SessionScope.ROLE, "judge").durable


def test_session_key_rejects_empty_identifier() -> None:
    with pytest.raises(ValueError, match="needs an identifier"):
        AgentSessionKey(SessionScope.ROLE, "")


def test_session_key_parse_rejects_missing_scope_prefix() -> None:
    with pytest.raises(ValueError, match="missing a scope prefix"):
        AgentSessionKey.parse("no-scope-here")


def test_agent_selection_is_a_pure_equality_comparable_value() -> None:
    first = AgentSelection(driver="agentshim", provider="codex", model="gpt-test")
    second = AgentSelection(driver="agentshim", provider="codex", model="gpt-test")

    assert first == second
    assert first.role_models == ()
