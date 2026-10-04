"""ObservationFactory: sequence and identity that core accepts, across retries and restarts."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.run_execution import run_execution_record

from vs_core.api import (
    AttemptId,
    AttemptRef,
    DecisionId,
    DiscardWorkspace,
    ObservationStatus,
    RequestId,
    Scope,
)
from vs_core.api.proofs import Proven, fresh_observation
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_runtime.api.core import (
    ObservationFactory,
    ObservationFacts,
    ObservationLedgerCorruptError,
    ObservationSubject,
)

if TYPE_CHECKING:
    from vs_core.api import Observation
    from vs_project.api import StateNamespace

_FACTS = (
    ObservationFacts(ObservationStatus.UNKNOWN, terminal=False, diagnostic="lease lost"),
    ObservationFacts(ObservationStatus.FAILED, terminal=False, diagnostic="try again"),
    ObservationFacts(ObservationStatus.SUCCEEDED, accepted=True, released=True),
    ObservationFacts(ObservationStatus.REJECTED, diagnostic="conflicting payload"),
)


def _namespace(tmp_path: Path) -> StateNamespace:
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    now = datetime(2026, 8, 11, tzinfo=UTC)
    project.state.create_project("Observations", now=now)
    manifest = project.state.new_run_manifest(
        "Observations",
        branch="vibesys/observations",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="team-search", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=now,
        unique=UUID(int=1),
    )
    project.state.create_run(manifest)
    return project.state.local_namespace(manifest.run_id, "receipts")


def _request(name: str) -> DiscardWorkspace:
    attempt = AttemptRef(attempt_id=AttemptId(root="a1"), generation=0)
    return DiscardWorkspace(
        request_id=RequestId(root=name),
        scope=Scope(owner=attempt.attempt_id, generation=0),
        admission_id=DecisionId(root="admit"),
        deadline_at=100.0,
        attempt=attempt,
    )


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


@given(steps=st.lists(st.tuples(st.sampled_from(_FACTS), st.booleans()), min_size=1, max_size=12))
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_every_issued_observation_is_fresh_for_core_across_restarts(
    steps: list[tuple[ObservationFacts, bool]],
) -> None:
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as env:
        env.setenv("VIBESYS_STATE_HOME", str(Path(directory) / "state"))
        namespace = _namespace(Path(directory))
        request = _request("request")
        factory = ObservationFactory(namespace)
        history: list[Observation] = []
        previous: ObservationFacts | None = None
        sequence = -1
        for index, (facts, restart) in enumerate(steps):
            if restart:
                factory = ObservationFactory(namespace)
            observation = factory.observe(
                ObservationSubject.of(request), facts, observed_at=float(index)
            )
            if facts != previous:
                sequence += 1
            previous = facts
            # Same facts replay the stored observation; new facts take the next sequence.
            assert observation.sequence == sequence
            assert observation.event_id.root == f"request:observation:{sequence}"
            assert observation.request_id == request.request_id
            assert observation.scope == request.scope
            assert observation.admission_id == request.admission_id
            assert isinstance(fresh_observation(tuple(history), observation, complete=True), Proven)
            history.append(observation)


def test_replay_returns_the_stored_observation_unchanged_even_at_a_later_time(
    tmp_path: Path,
) -> None:
    namespace = _namespace(tmp_path)
    request = _request("r1")
    first = ObservationFactory(namespace).observe(
        ObservationSubject.of(request), _FACTS[0], observed_at=1.0
    )
    assert (
        ObservationFactory(namespace).observe(
            ObservationSubject.of(request), _FACTS[0], observed_at=9.0
        )
        == first
    )


def test_requests_have_independent_sequences(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    factory = ObservationFactory(namespace)
    factory.observe(ObservationSubject.of(_request("r1")), _FACTS[0], observed_at=1.0)
    factory.observe(ObservationSubject.of(_request("r1")), _FACTS[2], observed_at=2.0)
    assert (
        factory.observe(ObservationSubject.of(_request("r2")), _FACTS[0], observed_at=3.0).sequence
        == 0
    )


def test_an_unreadable_row_is_a_typed_error_not_a_reused_sequence(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    request = _request("r1")
    factory = ObservationFactory(namespace)
    factory.observe(ObservationSubject.of(request), _FACTS[0], observed_at=1.0)
    (name,) = namespace.entries("observations")
    namespace.write_bytes(f"observations/{name}", b"not json")
    with pytest.raises(ObservationLedgerCorruptError):
        factory.observe(ObservationSubject.of(request), _FACTS[2], observed_at=2.0)
