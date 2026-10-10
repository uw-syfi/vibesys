"""A stopped warmup reaches the planner as the rate it achieved, not only as the stop's prose.

Regression for r19: ten warmups stopped at 7 to 16 tok/s, all with no partial measurement.
The candidate's benchmark is the bundle's own harness replaying r19's recorded warmup, so the
parse of the engine's stop text into a structured measurement is the production code's.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.dynamic_roles import ORCHESTRATOR

if TYPE_CHECKING:
    from tests.composition.dynamic._harness import Turn

_REPO = Path(__file__).resolve().parents[3]
# The real benchmark harness of the bundle whose warmup stops r19 recorded.
_BUNDLE_BENCHMARK = _REPO / "examples/model-serving/qwen3.5-9b-mi210/benchmark/run.py"
_WARMUP_STOP_STDERR = Path(__file__).with_name("golden") / "warmup_stop.stderr"

_REPLAY = """\
import importlib.util, pathlib, sys
namespace = {{}}
exec(pathlib.Path("queue.py").read_text(), namespace)
output = sys.argv[sys.argv.index("--vs-output") + 1]
if namespace.get("WARMUP_STOPS"):
    spec = importlib.util.spec_from_file_location("bundle_benchmark", {bundle!r})
    bundle = sys.modules["bundle_benchmark"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    report = bundle.ProtocolReport(pathlib.Path(output))
    watch = bundle.WarmupWatch(
        bundle.WARMUP_TIMEOUT_S, bundle.WARMUP_SESSION_CEILING_TOK_S, label="warmup sub-run"
    )
    try:
        bundle.run_session_runner(
            pathlib.Path({engine!r}),
            [],
            timeout_s=bundle.WARMUP_TIMEOUT_S,
            label="warmup sub-run",
            watch=watch.feed,
        )
    except bundle.HarnessError as error:
        report.fail(str(error), error.partial)
        raise SystemExit(1)
    raise SystemExit("the recorded warmup did not stop")
"""


def test_a_stopped_warmup_reaches_the_planner_as_its_rate(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path)
    # Stands in for session_runner: prints the recorded stderr, as the binary did up to the stop.
    engine = tmp_path / "recorded-session-runner"
    engine.write_text(f"#!/bin/sh\nexec cat {_WARMUP_STOP_STDERR} >&2\n", encoding="utf-8")
    engine.chmod(0o755)
    benchmark = loop_input.root / "benchmark.py"
    benchmark.write_text(
        _REPLAY.format(bundle=str(_BUNDLE_BENCHMARK), engine=str(engine))
        + benchmark.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def stop_at_warmup(agent: Turn) -> dict[str, object]:
        agent.write_queue("VALUE = 1\nWARMUP_STOPS = True\n")
        return implemented("W")

    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("W")), portfolio(workstream("X")))
        .implement("W", stop_at_warmup)
        .judge("W", PASS)
        .implement("X", implemented("X", outcome="blocked"))
    )

    run = run_request(
        loop_input.request(max_rounds=2, max_retries_per_round=1),
        agents,
        slurm_process=loop_input.connector,
        state_stores=loop_input.state_stores,
    )

    assert run.error is None
    assert agents.unscripted == []
    # r19's eval_eb00846: 1246 output tokens in 176 s, 9 of 72 rounds, against 14343 tokens in 180 s.
    measured = {
        "name": "warmup_output_tokens_per_s",
        "value": pytest.approx(1246 / 176),
        "direction": "max",
        "unit": "output tokens/s",
        "target": pytest.approx(14343 / 180),
        "completed": 9.0,
        "required": 72.0,
        "progress_unit": "rounds",
    }
    rounds = {
        item["hypothesis_id"]: item["rounds"]
        for item in CoreRecords(loop_input, run.run_id).strategy["hypotheses"]
    }
    (stopped,) = rounds["W"]
    assert stopped["benchmark_passed"] is False
    assert stopped["partial"] == measured
    planner = agents.prompts(ORCHESTRATOR.id)
    assert "partial warmup_output_tokens_per_s = 7.07954" in planner[1]
    assert "output tokens/s (better: max), pass requires 79.683" in planner[1]
    assert "progress 9 of 72 rounds" in planner[1]
