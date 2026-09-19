"""Measure revisioned experiment refresh projection and response size."""

# ruff: noqa: INP001, S101, S106

from __future__ import annotations

import os
import statistics
import tempfile
import threading
import time
from pathlib import Path

from server.api.experiments import build_experiment_log
from server.api.protocol import ExperimentCursor, ExperimentQuery, HypothesisEntry
from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.events import EventType, ExperimentsChangedData
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import EventJournal
from vibesys.loops.agent.hypotheses import append_round, reproject_run_evidence
from vibesys.loops.agent.model import AgentRunState, Hypothesis
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.schemas import OrchestratorPlan
from vs_loop_state import RoundRecord
from vs_project import AgentRunConfiguration, Project, RunEnvironmentRecord

SIZES = (20, 100, 500)
SAMPLES = 30


def _build_api(project: Project, run_id: str) -> tuple[RunApi, RunIntegrationAdapter, EventJournal]:
    condition = threading.Condition(threading.RLock())
    journal = EventJournal(condition)
    executions = ExecutionTracker(condition, journal)
    controller = RunController(condition, journal, executions)
    chat = ChatManager(condition, journal, run_status=controller.run_status)
    integration = RunIntegrationAdapter(controller, executions, journal, chat)
    api = RunApi(condition, controller, executions, journal, chat, integration)
    integration.attach(project.state.log_directory(run_id), project=project, run_id=run_id)
    return api, integration, journal


def _hypothesis(index: int) -> Hypothesis:
    identifier = f"H-{index:04d}"
    first_round = (index - 1) * 3 + 1
    return Hypothesis(
        hypothesis_id=identifier,
        plan=OrchestratorPlan(
            hypothesis_id=identifier,
            hypothesis=f"Claim {index}",
            task=f"Measure hypothesis {index}",
            pass_criteria="The benchmark improves.",
            reasoning="Synthetic benchmark entry.",
        ),
        started_round=first_round,
        rounds=[
            RoundRecord(
                round_number=first_round + offset,
                commit=f"{index:04x}{offset}",
                perf_metric=None,
                perf_unit=None,
                hypothesis_id=identifier,
                passed=True,
                reviewed=True,
            )
            for offset in range(3)
        ],
        last_experiment_revision=1,
    )


def _project(root: Path) -> tuple[Project, str]:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Benchmark experiment refresh.\n", encoding="utf-8")
    value = Project.open(root)
    value.state.create_project("benchmark")
    manifest = value.state.new_run_manifest(
        "benchmark",
        run_id="benchmark-run",
        branch="vibesys/benchmark-run",
        vibesys_version="benchmark",
        configuration=AgentRunConfiguration(
            outer_loop="agent",
            inner_loop="single-agent",
            interface="inprocess",
            agent_backend="stub",
            compute_backend="cpu",
            profiler="none",
            max_rounds=500,
            max_retries_per_round=1,
            judge_every=1,
            official_eval_every=1,
            memory_layout="files",
            run_environment=RunEnvironmentRecord(name="local"),
        ),
        trusted_input_baseline="0" * 40,
    )
    value.state.create_run(manifest)
    return value, manifest.run_id


def _measure(count: int) -> tuple[float, float, float, int, int, int]:
    with tempfile.TemporaryDirectory(prefix="vibesys-experiment-refresh-") as directory:
        os.environ["VIBESYS_STATE_HOME"] = str(Path(directory) / "state")
        value, run_id = _project(Path(directory) / "project")
        state = reproject_run_evidence(
            AgentRunState(
                experiment_revision=1,
                active_hypothesis_id=f"H-{count:04d}",
                hypotheses=[_hypothesis(index) for index in range(1, count + 1)],
            )
        )
        AgentRunStateStore(value.state.portable_namespace(run_id, "agent")).save(state)
        api, integration, journal = _build_api(value, run_id)

        started = time.perf_counter_ns()
        full = api.execute(ExperimentQuery())
        full_ms = (time.perf_counter_ns() - started) / 1_000_000
        full_bytes = len(full.model_dump_json().encode())
        projected_entries = {entry.hypothesis_id: entry for entry in full.experiments}
        update = full.experiment_update
        assert update is not None
        cursor = ExperimentCursor(
            run_id=run_id,
            projection_id=update.projection_id,
            revision=update.through_revision,
        )

        unchanged_times: list[float] = []
        changed_times: list[float] = []
        unchanged_bytes = delta_bytes = 0
        for sample in range(SAMPLES):
            started = time.perf_counter_ns()
            unchanged = api.execute(ExperimentQuery(after=cursor))
            unchanged_times.append((time.perf_counter_ns() - started) / 1_000_000)
            unchanged_bytes = len(unchanged.model_dump_json().encode())

            changed_id = state.hypotheses[-1].hypothesis_id
            state = append_round(
                state,
                RoundRecord(
                    round_number=count * 3 + sample + 1,
                    commit=f"delta-{sample}",
                    perf_metric=None,
                    perf_unit=None,
                    hypothesis_id=changed_id,
                    passed=sample % 2 == 0,
                    reviewed=True,
                ),
                keep_active=True,
            )
            started = time.perf_counter_ns()
            integration.publish_committed_state(
                "agent",
                state,
                changed_keys=(changed_id,),
            )
            journal.record(
                EventType.EXPERIMENTS_CHANGED,
                data=ExperimentsChangedData(
                    reason="round_persisted",
                    revision=state.experiment_revision,
                ),
            )
            delta = api.execute(ExperimentQuery(after=cursor))
            changed_times.append((time.perf_counter_ns() - started) / 1_000_000)
            delta_bytes = len(delta.model_dump_json().encode())
            assert delta.experiment_update is not None
            assert len(delta.experiments) == 1
            projected_entries[changed_id] = delta.experiments[0]
            fresh = build_experiment_log(state)
            assert _ordered(projected_entries) == fresh
            assert delta.experiments[0].rounds[-1].round == count * 3 + sample + 1
            cursor = cursor.model_copy(
                update={"revision": delta.experiment_update.through_revision}
            )

        return (
            full_ms,
            statistics.median(unchanged_times),
            statistics.median(changed_times),
            full_bytes,
            unchanged_bytes,
            delta_bytes,
        )


def _ordered(entries: dict[str, HypothesisEntry]) -> list[HypothesisEntry]:
    return sorted(entries.values(), key=lambda entry: (entry.first_round, entry.hypothesis_id))


def main() -> None:
    """Print a Markdown table suitable for the pull request."""
    print(  # noqa: T201
        "| hypotheses | full projection ms | unchanged refresh ms | "
        "one-change refresh ms | full bytes | unchanged bytes | delta bytes |"
    )
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |")  # noqa: T201
    for count in SIZES:
        full_ms, unchanged_ms, changed_ms, full_bytes, unchanged_bytes, delta_bytes = _measure(
            count
        )
        print(  # noqa: T201
            f"| {count} | {full_ms:.3f} | {unchanged_ms:.3f} | {changed_ms:.3f} | "
            f"{full_bytes} | {unchanged_bytes} | {delta_bytes} |"
        )


if __name__ == "__main__":
    main()
