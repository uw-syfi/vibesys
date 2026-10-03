"""The dynamic state survives the project's real, strict state store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.run_execution import run_execution_record
from tests.vibesys.orchestration.dynamic._support import (
    INPUT_BASELINE,
    Script,
    baseline_run,
    dynamic_options,
    implementation,
    portfolio,
    throughput,
)

from vibesys.orchestration.dynamic import (
    PLUGIN,
    DynamicState,
    EvidenceReference,
    ImplementerResult,
)
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import WorkstreamPhase, WorkstreamPlan
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)

if TYPE_CHECKING:
    from vs_project.api import StateNamespace


def _namespace(tmp_path: Path) -> StateNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run = project.state.new_run_manifest(
        "Run 1",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
    )
    project.state.create_run(run)
    return project.state.local_namespace("run-1", "dynamic")


def _finished_state(tmp_path: Path) -> DynamicState:
    script = Script(
        {
            ORCHESTRATOR.id: [portfolio("kept")],
            IMPLEMENTER.id: [implementation("kept")],
            JUDGE.id: [{"passed": True, "analysis": "Candidate is correct."}],
        }
    )

    async def scenario() -> DynamicState | None:
        run = baseline_run(tmp_path, script)
        run.evaluation.script_benchmark(INPUT_BASELINE, throughput(2.0))
        await PLUGIN.orchestrate(run, dynamic_options(max_in_flight=1))
        return await run.state.load(DynamicState)

    state = asyncio.run(scenario())
    assert state is not None
    plan = state.workstreams[0].plan
    plan.evidence = (EvidenceReference(location="profile/trace.json", purpose="hot kernel"),)
    return state


def test_state_with_tuple_fields_round_trips_through_the_project_state_store(
    tmp_path: Path,
) -> None:
    """The store loads with ``strict=True``, which rejects JSON arrays for tuple
    fields when a ``mode="before"`` validator forces Python-mode validation.
    """
    state = _finished_state(tmp_path / "scenario")
    namespace = _namespace(tmp_path / "project")

    namespace.save("state.json", state)

    assert namespace.load("state.json", DynamicState) == state
    assert namespace.load("state.json", DynamicState).workstreams[0].plan.evidence


def test_empty_state_round_trips_through_the_project_state_store(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)

    namespace.save("state.json", DynamicState())

    assert namespace.load("state.json", DynamicState) == DynamicState()


def test_state_written_by_an_older_version_loads_through_the_project_state_store(
    tmp_path: Path,
) -> None:
    current = _finished_state(tmp_path / "scenario")
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 3
    for item in legacy["workstreams"]:
        budget = item.pop("budget")
        item["attempts"], item["refunded_attempts"] = budget["spent"], budget["refunded"]
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    assert namespace.load("state.json", DynamicState) == current


def test_version_5_state_with_a_validation_recipe_artifact_loads_without_it(
    tmp_path: Path,
) -> None:
    current = _finished_state(tmp_path / "scenario")
    legacy = current.model_dump(mode="json")
    legacy["schema_version"] = 5
    assert legacy["workstreams"][0]["implementation"] is not None
    legacy["workstreams"][0]["implementation"]["validation_recipe_artifact"] = "validation/r.json"
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    loaded = namespace.load("state.json", DynamicState)

    assert loaded == current
    assert loaded.schema_version == 7


@pytest.mark.parametrize("version", [6, 7])
def test_current_state_with_a_validation_recipe_artifact_is_rejected(
    tmp_path: Path, version: int
) -> None:
    current = _finished_state(tmp_path / "scenario")
    stale = current.model_dump(mode="json")
    stale["schema_version"] = version
    stale["workstreams"][0]["implementation"]["validation_recipe_artifact"] = "validation/r.json"
    namespace = _namespace(tmp_path / "project")
    (namespace.external_directory() / "state.json").write_text(json.dumps(stale), encoding="utf-8")

    with pytest.raises(ProjectStateError, match="validation_recipe_artifact"):
        namespace.load("state.json", DynamicState)


def test_implementer_result_schema_has_no_validation_recipe_field() -> None:
    schema = ImplementerResult.model_json_schema()

    assert "validation_recipe_artifact" not in schema["properties"]
    with pytest.raises(ValueError, match="validation_recipe_artifact"):
        ImplementerResult.model_validate(
            {**implementation("x"), "validation_recipe_artifact": "validation/r.json"}
        )


@pytest.mark.parametrize("unknown_key", ["reason", "retry_forever", "attempt_count"])
def test_input_measurement_state_rejects_unknown_keys(unknown_key: str) -> None:
    encoded = json.dumps(
        {
            "schema_version": 6,
            "input_measurement": {"revision": "input", "attempts": 1, unknown_key: True},
        }
    )
    with pytest.raises(ValidationError, match=unknown_key):
        DynamicState.model_validate_json(encoded, strict=True)


@pytest.mark.parametrize("version", [6, 7])
@pytest.mark.parametrize("unknown_key", ["eligible_evaluation_candidates", "retry_forever"])
def test_version_6_migration_does_not_discard_unknown_keys(
    tmp_path: Path, version: int, unknown_key: str
) -> None:
    namespace = _namespace(tmp_path)
    encoded = json.dumps({"schema_version": version, unknown_key: True})
    (namespace.external_directory() / "state.json").write_text(encoded, encoding="utf-8")

    with pytest.raises(ProjectStateError, match=unknown_key):
        namespace.load("state.json", DynamicState)


@given(
    counters=st.tuples(
        st.integers(min_value=1, max_value=100),
        st.integers(min_value=0, max_value=1000),
        st.integers(min_value=0, max_value=12000000),
    ),
    phase=st.sampled_from([WorkstreamPhase.PARKED, WorkstreamPhase.CANCELLED]),
    priority=st.sampled_from(["now", "next", "later"]),
    minutes=st.floats(min_value=2, max_value=240, allow_nan=False, allow_infinity=False),
)
def test_agent_state_and_new_phases_round_trip_through_the_project_state_store(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    counters: tuple[int, int, int],
    phase: WorkstreamPhase,
    priority: str,
    minutes: float,
) -> None:
    generation, turns, tokens = counters
    encoded = json.loads(
        (Path(__file__).parent / "fixtures" / "state_v6" / "completed.json").read_bytes()
    )
    encoded["schema_version"] = 7
    encoded["workstreams"][0]["phase"] = phase.value
    plan = encoded["workstreams"][0]["plan"]
    expectation = {"milestone": "benchmark passes", "expected_minutes": minutes, "reason": "test"}
    encoded["agent"] = {
        "generation": generation,
        "turns": turns,
        "input_tokens": tokens,
        "output_tokens": tokens,
        "expectations": {"kept": expectation},
        "queue": [{"spec": plan, "expectation": expectation, "priority": priority}],
        "steers": {
            "kept": [
                {
                    "note_sha256": "a" * 64,
                    "text": "Continue",
                    "sent_at_s": 30.0,
                    "interrupt": False,
                    "delivered_to": "turn-1",
                }
            ]
        },
        "journal": [
            {"at_s": 30.0, "turn": turns, "kind": "steer", "subject": "kept", "text": "Continue"}
        ],
        "next_check_in_s": 120.0,
        "capabilities_withdrawn": ["profile"],
        "finished": None,
    }
    namespace = _namespace(tmp_path_factory.mktemp("agent-state"))
    (namespace.external_directory() / "state.json").write_text(
        json.dumps(encoded), encoding="utf-8"
    )

    loaded = namespace.load("state.json", DynamicState)
    assert loaded.agent is not None
    assert loaded.workstreams[0].phase is phase
    assert isinstance(loaded.agent.queue[0].spec, WorkstreamPlan)
    assert loaded.agent.queue[0].spec.evidence
    namespace.save("state.json", loaded)
    assert namespace.load("state.json", DynamicState) == loaded


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ({"generation": 0}, "generation"),
        ({"turns": -1}, "turns"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"output_tokens": -1}, "output_tokens"),
        ({"capabilities_withdrawn": ["implement"]}, "capabilities_withdrawn"),
        ({"unknown": True}, "unknown"),
        (
            {
                "expectations": {
                    "kept": {"milestone": "pass", "expected_minutes": 1, "reason": "test"}
                }
            },
            "expected_minutes",
        ),
        (
            {
                "expectations": {
                    "kept": {"milestone": "pass", "expected_minutes": 241, "reason": "test"}
                }
            },
            "expected_minutes",
        ),
        (
            {
                "expectations": {
                    "kept": {
                        "milestone": "pass",
                        "expected_minutes": 2,
                        "reason": "test",
                        "unknown": True,
                    }
                }
            },
            "unknown",
        ),
        (
            {
                "steers": {
                    "kept": [
                        {"note_sha256": "a" * 64, "text": "", "sent_at_s": 0, "interrupt": False}
                    ]
                }
            },
            "text",
        ),
        (
            {
                "steers": {
                    "kept": [
                        {
                            "note_sha256": "a" * 64,
                            "text": "x" * 2001,
                            "sent_at_s": 0,
                            "interrupt": False,
                        }
                    ]
                }
            },
            "text",
        ),
        (
            {
                "steers": {
                    "kept": [
                        {
                            "note_sha256": "a" * 64,
                            "text": "continue",
                            "sent_at_s": 0,
                            "interrupt": False,
                            "unknown": True,
                        }
                    ]
                }
            },
            "unknown",
        ),
        (
            {
                "steers": {
                    "kept": [
                        {
                            "note_sha256": "a" * 64,
                            "text": "continue",
                            "sent_at_s": 0,
                            "interrupt": False,
                            "dropped": "lost",
                        }
                    ]
                }
            },
            "dropped",
        ),
        (
            {
                "journal": [
                    {"at_s": 0, "turn": 1, "kind": "restart", "subject": None, "text": "test"}
                ]
            },
            "kind",
        ),
        (
            {
                "journal": [
                    {
                        "at_s": 0,
                        "turn": 1,
                        "kind": "start",
                        "subject": None,
                        "text": "test",
                        "unknown": True,
                    }
                ]
            },
            "unknown",
        ),
        ({"queue": [{"unknown": True}]}, "unknown"),
    ],
)
def test_agent_state_rejects_invalid_values(
    tmp_path: Path, value: dict[str, object], field: str
) -> None:
    namespace = _namespace(tmp_path)
    encoded = json.dumps({"schema_version": 7, "agent": value})
    (namespace.external_directory() / "state.json").write_text(encoded, encoding="utf-8")

    with pytest.raises(ProjectStateError, match=field):
        namespace.load("state.json", DynamicState)
