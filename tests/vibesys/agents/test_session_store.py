"""Durable provider-session checkpoints and AgentClient resume on restart.

These tests exercise the machine-local session store in isolation and its
integration with :class:`~vibesys.agents.client.AgentClient`: a fresh client
(standing in for a resumed process) must offer a checkpointed provider session
ID to the very first turn, but only when the spec that produced it still
matches, and it must forget the ID when a driver reports a restart or refuses
to adopt it. A broken store must never cost a completed turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vibesys.agents.client import AgentClient
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentObserver,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    SessionDisposition,
    session_spec_fingerprint,
)
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.agents.session_store import (
    AgentSessionState,
    DurableSessionStore,
    NullSessionStore,
)
from vs_project import (
    PlainRunConfiguration,
    Project,
    RunEnvironmentRecord,
)

HYPOTHESIS = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")
ROLE = AgentSessionKey(SessionScope.ROLE, "judge")


def _project(tmp_path: Path) -> Project:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "test",
        run_id="run-1",
        branch="vibesys/run-1",
        vibesys_version="test",
        trusted_input_baseline="a" * 40,
        configuration=PlainRunConfiguration(
            outer_loop="plain",
            run_environment=RunEnvironmentRecord(name="local"),
            agent_backend="stub",
            compute_backend="cpu",
            max_rounds=2,
            max_attempts_per_issue=1,
            max_issues_per_perf_eval=1,
        ),
    )
    project.state.create_run(run)
    return project


def _slot_path(project: Project) -> Path:
    return project.state.local_namespace("run-1", "agent").external_directory() / "sessions.json"


def _store(tmp_path: Path, log: list[str] | None = None) -> DurableSessionStore:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    if log is None:
        return DurableSessionStore(slot)
    return DurableSessionStore(slot, log=log.append)


def _spec(*, provider: str = "codex", model: str = "gpt-5.6-sol") -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider=provider,
        model=model,
        workspace=Path("/workspace"),
        policy=AgentExecutionPolicy(),
        skills=(),
    )


def _checkpoint(
    store: DurableSessionStore,
    session_id: str,
    *,
    key: AgentSessionKey = HYPOTHESIS,
    spec: AgentSessionSpec | None = None,
) -> None:
    """Write the checkpoint a completed turn on ``spec`` would have written."""
    spec = spec if spec is not None else _spec()
    store.record(
        key,
        spec_fingerprint=session_spec_fingerprint(spec),
        provider=spec.provider,
        model=spec.model,
        session_id=session_id,
        role=spec.role,
    )


# --- session keys ----------------------------------------------------------


def test_session_key_serializes_to_the_stored_form() -> None:
    assert str(HYPOTHESIS) == "hypothesis:H-01"
    assert AgentSessionKey.parse("hypothesis:H-01") == HYPOTHESIS


def test_only_run_scoped_conversations_are_durable() -> None:
    assert HYPOTHESIS.durable
    assert AgentSessionKey(SessionScope.CHAT, "thread-9").durable
    assert not ROLE.durable


@pytest.mark.parametrize("stored", ["implementer", "unknown-scope:x", "hypothesis:"])
def test_unparseable_keys_are_rejected(stored: str) -> None:
    with pytest.raises(ValueError, match=r"scope|identifier"):
        AgentSessionKey.parse(stored)


# --- DurableSessionStore ---------------------------------------------------


def test_record_then_get_returns_the_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _checkpoint(store, "thread-abc")
    record = store.get(HYPOTHESIS)

    assert record is not None
    assert record.session_id == "thread-abc"
    assert record.spec_fingerprint == session_spec_fingerprint(_spec())
    assert record.provider == "codex"
    assert record.model == "gpt-5.6-sol"
    assert record.role == "implementer"


def test_get_misses_return_none(tmp_path: Path) -> None:
    assert _store(tmp_path).get(AgentSessionKey(SessionScope.HYPOTHESIS, "absent")) is None


def test_record_replaces_the_previous_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _checkpoint(store, "s1")
    _checkpoint(store, "s2")

    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "s2"


def test_checkpoint_survives_a_new_store_instance(tmp_path: Path) -> None:
    project = _project(tmp_path)
    namespace = project.state.local_namespace("run-1", "agent")

    _checkpoint(DurableSessionStore(namespace.slot("sessions.json", AgentSessionState)), "thr-xyz")
    # A fresh instance over the same slot models a resumed process: the on-disk
    # map, not any in-memory cache, is the source of truth.
    reloaded = DurableSessionStore(namespace.slot("sessions.json", AgentSessionState)).get(
        HYPOTHESIS
    )

    assert reloaded is not None
    assert reloaded.session_id == "thr-xyz"


def test_clear_forgets_only_the_named_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    keep = AgentSessionKey(SessionScope.HYPOTHESIS, "keep")
    _checkpoint(store, "s-keep", key=keep)
    _checkpoint(store, "s-drop")

    store.clear(HYPOTHESIS)

    assert store.get(HYPOTHESIS) is None
    assert store.get(keep) is not None


def test_clear_of_missing_key_is_a_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.clear(HYPOTHESIS)  # must not raise
    assert store.get(HYPOTHESIS) is None


def test_role_scoped_conversations_are_never_persisted(tmp_path: Path) -> None:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    store = DurableSessionStore(slot)

    _checkpoint(store, "judge-thread", key=ROLE)

    # Nothing was written at all: an unscoped role conversation belongs to the
    # process that created it, so a later run must not resume it.
    assert store.get(ROLE) is None
    assert not _slot_path(project).exists()


# --- store failures are non-fatal -----------------------------------------


def test_malformed_map_degrades_to_no_persistence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    _slot_path(project).write_text("{not json", encoding="utf-8")
    log: list[str] = []
    store = DurableSessionStore(slot, log=log.append)

    assert store.get(HYPOTHESIS) is None
    assert any("unreadable" in message for message in log)

    # The next completed turn replaces the broken file rather than inheriting it.
    _checkpoint(store, "thread-new")
    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-new"


def test_map_written_by_a_foreign_key_contract_is_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    # A role-scoped key could only come from a different contract; the whole
    # map is refused rather than partially trusted.
    _slot_path(project).write_text(
        '{"schema_version": 1, "sessions": {"role:judge": '
        '{"spec_fingerprint": "f", "session_id": "s", "provider": "codex"}}}',
        encoding="utf-8",
    )
    log: list[str] = []

    assert DurableSessionStore(slot, log=log.append).get(HYPOTHESIS) is None
    assert log


def test_unwritable_slot_does_not_fail_the_turn(tmp_path: Path) -> None:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    # A directory where the map belongs makes every write fail.
    _slot_path(project).mkdir(parents=True)
    log: list[str] = []
    store = DurableSessionStore(slot, log=log.append)

    _checkpoint(store, "thread-1")  # must not raise

    assert any("could not checkpoint" in message for message in log)


def test_a_broken_store_never_discards_a_completed_turn(tmp_path: Path) -> None:
    project = _project(tmp_path)
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    _slot_path(project).mkdir(parents=True)
    store = DurableSessionStore(slot, log=lambda _message: None)
    session = _FakeSession(results=[AgentTurnResult("done", provider_session_id="thread-1")])
    client = AgentClient(_FakeDriver([session]), session_store=store)

    result = client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=HYPOTHESIS)

    assert result.text == "done"


# --- NullSessionStore ------------------------------------------------------


def test_null_store_persists_nothing_and_always_misses() -> None:
    store = NullSessionStore()

    store.record(
        HYPOTHESIS, spec_fingerprint="f", provider="codex", model="m", session_id="s", role="r"
    )
    store.clear(HYPOTHESIS)

    assert store.get(HYPOTHESIS) is None


# --- AgentClient resume ----------------------------------------------------


@dataclass
class _FakeSession:
    """A fake session that records adoption attempts and queued results."""

    results: list[AgentTurnResult]
    error: Exception | None = None
    adopts: bool = True
    offered: list[str] = field(default_factory=list)
    close_calls: int = 0

    def run_turn(
        self,
        request: AgentTurnRequest,  # noqa: ARG002
        observer: AgentObserver | None = None,  # noqa: ARG002
    ) -> AgentTurnResult:
        if self.error is not None:
            raise self.error
        return self.results.pop(0)

    def resume_provider_session(self, session_id: str) -> bool:
        self.offered.append(session_id)
        return self.adopts

    def cancel(self) -> None:
        """Accept the contract's cancel; the fake has no in-flight turn."""

    def close(self) -> None:
        self.close_calls += 1


@dataclass
class _FakeDriver:
    queued_sessions: list[_FakeSession]
    specs: list[AgentSessionSpec] = field(default_factory=list)
    close_calls: int = 0

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities()

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        self.specs.append(spec)
        return self.queued_sessions.pop(0)

    def close(self) -> None:
        self.close_calls += 1


def test_completed_turn_checkpoints_its_provider_session_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _FakeSession(results=[AgentTurnResult("done", provider_session_id="thread-1")])
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=HYPOTHESIS)

    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-1"
    assert record.spec_fingerprint == session_spec_fingerprint(_spec())


def test_role_scoped_turn_checkpoints_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _FakeSession(results=[AgentTurnResult("done", provider_session_id="thread-1")])
    client = AgentClient(_FakeDriver([session]), session_store=store)

    # No session_key: the client falls back to a role-scoped key.
    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"))

    assert store.get(AgentSessionKey(SessionScope.ROLE, "implementer")) is None


def test_turn_without_a_session_id_keeps_the_prior_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "thread-old")
    session = _FakeSession(results=[AgentTurnResult("done", provider_session_id=None)])
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=HYPOTHESIS)

    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-old"


def test_fresh_client_offers_the_checkpoint_to_its_first_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "thread-1")
    session = _FakeSession(results=[AgentTurnResult("resumed", provider_session_id="thread-1")])
    # A brand new client with no in-memory session models a resumed process.
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("continue"), session_key=HYPOTHESIS)

    assert session.offered == ["thread-1"]


def test_a_reused_live_session_is_not_offered_the_checkpoint_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _FakeSession(
        results=[
            AgentTurnResult("one", provider_session_id="thread-1"),
            AgentTurnResult("two", provider_session_id="thread-1"),
        ]
    )
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=HYPOTHESIS)
    client.run(session_spec=_spec(), turn=AgentTurnRequest("two"), session_key=HYPOTHESIS)

    # The live conversation is already the newest history there is.
    assert session.offered == []


def test_checkpoint_from_a_different_spec_is_refused_and_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Reasoning effort is not provider or model, but it is part of the spec that
    # in-process reuse compares, so it must also refuse a checkpoint.
    other = AgentSessionSpec(
        role="implementer",
        provider="codex",
        model="gpt-5.6-sol",
        workspace=Path("/workspace"),
        policy=AgentExecutionPolicy(),
        skills=(),
        reasoning_effort="high",
    )
    _checkpoint(store, "thread-1", spec=other)
    session = _FakeSession(results=[AgentTurnResult("fresh", provider_session_id="thread-2")])
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("go"), session_key=HYPOTHESIS)

    assert session.offered == []
    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-2"


def test_a_refused_adoption_drops_the_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "thread-1")
    # A driver whose provider cannot resume (omnigent today) reports False.
    session = _FakeSession(results=[AgentTurnResult("fresh")], adopts=False)
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("go"), session_key=HYPOTHESIS)

    assert session.offered == ["thread-1"]
    assert store.get(HYPOTHESIS) is None


def test_reset_disposition_evicts_and_clears_the_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "thread-1")
    session = _FakeSession(
        results=[AgentTurnResult("reset", disposition=SessionDisposition.RESET_REQUIRED)]
    )
    client = AgentClient(_FakeDriver([session]), session_store=store)

    client.run(session_spec=_spec(), turn=AgentTurnRequest("one"), session_key=HYPOTHESIS)

    assert store.get(HYPOTHESIS) is None
    assert session.close_calls == 1


def test_a_failed_turn_keeps_the_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _checkpoint(store, "thread-1")
    # A timeout or a cancelled run says nothing about whether the conversation
    # is still resumable, so the checkpoint outlives it.
    failing = _FakeSession(results=[], error=RuntimeError("transient network error"))
    client = AgentClient(_FakeDriver([failing]), session_store=store)

    with pytest.raises(RuntimeError):
        client.run(session_spec=_spec(), turn=AgentTurnRequest("go"), session_key=HYPOTHESIS)

    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-1"


def test_in_process_spec_change_replaces_the_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _FakeSession(results=[AgentTurnResult("one", provider_session_id="thread-1")])
    second = _FakeSession(results=[AgentTurnResult("two", provider_session_id="thread-2")])
    client = AgentClient(_FakeDriver([first, second]), session_store=store)

    client.run(
        session_spec=_spec(model="gpt-5.6-sol"),
        turn=AgentTurnRequest("one"),
        session_key=HYPOTHESIS,
    )
    # A within-process model change evicts the live session; the new session
    # refuses the stale checkpoint and writes its own.
    client.run(
        session_spec=_spec(model="gpt-6"),
        turn=AgentTurnRequest("two"),
        session_key=HYPOTHESIS,
    )

    assert second.offered == []
    record = store.get(HYPOTHESIS)
    assert record is not None
    assert record.session_id == "thread-2"
    assert record.model == "gpt-6"
