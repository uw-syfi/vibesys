"""A reply the provider or the schema refuses reaches core as typed errors that keep the validator's words.

The planner is corrected from these errors: the field that failed and what the validator said of it
(an id that is not canonical, the provider's last schema errors). The turn ends failed but accepted,
never unknown, so core corrects the conversation instead of asking the provider again.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict
from tests.support.session_world import (
    SCHEMA,
    SessionHost,
    dispatch_request,
    ensure_request,
    open_host,
)

from vs_agent.api import AgentClient, AgentOutputSchemaError
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_core.api import ObservationStatus, ReplyErrorCode
from vs_project.api import Project
from vs_runtime.api import AgentId
from vs_runtime.api.core import ExecutionResult, ReceiptStore
from vs_sim.api.testing import ManualClock, SimThreads, examples, run_virtual


class Named(BaseModel):
    """A reply that carries one id an agent chose."""

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: AgentId


_ERRORS = st.text(st.characters(codec="ascii", categories=("L", "N", "P", "Zs")), min_size=1)


async def _turn(host: SessionHost, base: Path, threads: SimThreads) -> ExecutionResult:
    """Bind the session of ``host`` and dispatch one turn of it, on a store of its own."""
    (base / "project").mkdir()
    store = ReceiptStore(
        Project.open(base / "project").state.state_store_namespace("run"), threads=threads
    )
    await host.run(ensure_request(), store)
    return await host.run(dispatch_request(), store)


def _dispatch(host: SessionHost, threads: SimThreads | None = None) -> ExecutionResult:
    """Dispatch one turn; its blocking work runs as simulated threads of ``threads``."""
    host.resolver.schemas[SCHEMA] = Named
    sim = SimThreads() if threads is None else threads
    with tempfile.TemporaryDirectory() as raw:
        return run_virtual(sim.clock, _turn(host, Path(raw), sim), driver=sim)


def _failed_with_errors(result: ExecutionResult) -> tuple[object, ...]:
    observed = result.observation.observation
    assert observed.status is ObservationStatus.FAILED
    assert observed.accepted
    (event,) = result.owner_events
    assert event.reply_errors == result.observation.reply_errors  # ty: ignore[unresolved-attribute]
    return result.observation.reply_errors


@settings(max_examples=examples(pr=5, nightly=10))
@given(
    identifier=st.text(st.characters(categories=("L", "N")), min_size=1, max_size=20),
    padding=st.integers(1, 3),
)
def test_a_reply_with_a_padded_id_fails_at_the_id_with_the_validators_words(
    identifier: str, padding: int
) -> None:
    padded = identifier + " " * padding
    with tempfile.TemporaryDirectory() as raw:
        host = open_host(Path(raw), answer={"hypothesis_id": padded})
        errors = _failed_with_errors(_dispatch(host))

    (error,) = errors
    assert error.path == ("hypothesis_id",)  # ty: ignore[unresolved-attribute]
    assert error.code is ReplyErrorCode.INVALID_VALUE  # ty: ignore[unresolved-attribute]
    assert f"{padded!r} is not a valid identifier" in error.message  # ty: ignore[unresolved-attribute]


@settings(max_examples=examples(pr=5, nightly=10))
@given(reason=_ERRORS)
def test_a_provider_that_gave_up_on_the_schema_hands_core_its_last_errors_whole(
    reason: str,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = open_host(Path(raw))
        script = FakeTurnScript((AgentOutputSchemaError(reason),))
        sim = SimThreads()
        host.client = AgentClient(
            FakeProvider(script=script, threads=sim), clock=ManualClock(), threads=sim
        )
        errors = _failed_with_errors(_dispatch(host, sim))

    (error,) = errors
    assert error.path == ()  # ty: ignore[unresolved-attribute]
    assert error.message == reason  # ty: ignore[unresolved-attribute]


def test_the_answer_of_a_valid_reply_is_not_an_error() -> None:
    with tempfile.TemporaryDirectory() as raw:
        host = open_host(Path(raw), answer={"hypothesis_id": "h1"})
        result = _dispatch(host)

    assert result.observation.observation.status is ObservationStatus.SUCCEEDED
    assert result.observation.reply_errors == ()
    (event,) = result.owner_events
    assert json.loads(event.output_json or "") == {"hypothesis_id": "h1"}  # ty: ignore[unresolved-attribute]
