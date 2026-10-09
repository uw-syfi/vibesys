"""A candidate that crashes the benchmark fails with its crash text instead of burning the budget.

An agent that raises the batch size or the KV cache fraction can make the benchmark's
server run out of memory. Whether that is the candidate's fault depends on what the
evaluator left behind: a process killed with no result record might be a fluke (it is
measured once more), one that wrote its own error record is the candidate's (measured
once). Either way the planner reads the crash text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.composition.dynamic._harness import (
    PASS,
    CoreRecords,
    LoopInput,
    ScriptedAgents,
    edit_to,
    implemented,
    portfolio,
    run_request,
    workstream,
)

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path

_CRASH = "HIP out of memory: tried to allocate 20.00 GiB (GPU 0)"
_CRASHING_VALUE = 7


# The benchmark of a candidate whose server is killed: it prints why to stderr and dies
# with the kernel's out-of-memory status before it writes a result record.
def _killed(message: str) -> str:
    return (
        "import pathlib, sys\n"
        'if "VALUE = 7" in pathlib.Path("queue.py").read_text():\n'
        f"    sys.stderr.write({message!r} + chr(10))\n"
        "    raise SystemExit(137)\n"
    )


_KILLED = _killed(_CRASH)
# The benchmark of a candidate whose evaluator survives its server's crash and says so.
_REPORTED = (
    "import json, pathlib, sys\n"
    'if "VALUE = 7" in pathlib.Path("queue.py").read_text():\n'
    '    output = sys.argv[sys.argv.index("--vs-output") + 1]\n'
    '    hello = {"kind": "hello", "protocol": 2, "metrics": {"throughput": {"direction": "max"}}}\n'
    f'    error = {{"kind": "error", "message": {_CRASH!r}}}\n'
    '    pathlib.Path(output).write_text("".join(json.dumps(r) + chr(10) for r in (hello, error)))\n'
    "    raise SystemExit(139)\n"
)


def _crash_on_seven(loop_input: LoopInput, prefix: str) -> None:
    benchmark = loop_input.root / "benchmark.py"
    benchmark.write_text(prefix + benchmark.read_text(encoding="utf-8"), encoding="utf-8")


def _search(loop_input: LoopInput) -> tuple[ScriptedAgents, CoreRecords]:
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A")), portfolio(workstream("B")))
        .implement("A", edit_to(_CRASHING_VALUE, "A"))
        .judge("A", PASS)
        .implement("B", implemented("B", outcome="blocked"))
    )
    run = run_request(loop_input.request(max_rounds=2), agents)
    assert run.error is None
    assert agents.unscripted == []
    return agents, CoreRecords(loop_input, run.run_id)


@pytest.mark.parametrize(
    ("prefix", "measurements"),
    [(_KILLED, 2), (_REPORTED, 1)],
    ids=["killed-without-a-record", "reported-its-own-crash"],
)
def test_a_crashing_candidate_is_measured_by_its_class_and_the_planner_reads_the_crash(
    tmp_path: Path, prefix: str, measurements: int
) -> None:
    loop_input = LoopInput.create(tmp_path)
    _crash_on_seven(loop_input, prefix)

    agents, records = _search(loop_input)

    # One job measures the input, then the candidate is measured once, or once more if
    # its failure could have been the machinery's.
    assert loop_input.sbatch_count() == 1 + measurements
    attempt = records.attempt("A")
    assert attempt["measurements"] == measurements
    # The planner that writes the next workstream reads why the candidate failed.
    assert _CRASH in agents.prompts(ORCHESTRATOR.id)[-1]


def test_a_crash_text_with_a_code_fence_stays_inside_the_fence_it_is_shown_in(
    tmp_path: Path,
) -> None:
    message = f"{_CRASH} ```python"
    loop_input = LoopInput.create(tmp_path)
    _crash_on_seven(loop_input, _killed(message))

    agents, _records = _search(loop_input)

    lines = agents.prompts(ORCHESTRATOR.id)[-1].splitlines()
    crash = next(i for i, line in enumerate(lines) if message in line)
    fences = [i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {"`"}]
    before = max(i for i in fences if i < crash)
    after = min(i for i in fences if i > crash)
    # The text holds a run of three backticks, so the fence around it is longer.
    assert lines[before].strip() == lines[after].strip()
    assert len(lines[before].strip()) > 3
