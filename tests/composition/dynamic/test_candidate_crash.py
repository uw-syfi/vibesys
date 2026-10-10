"""A candidate that crashes the benchmark fails with its crash text instead of burning the budget.

An agent that raises the batch size or the KV cache fraction can make the benchmark's
server run out of memory. Whether that is the candidate's fault depends on what the
evaluator left behind: a process killed with no result record might be a fluke (it is
measured once more), one that wrote its own error record is the candidate's (measured
once). Either way the planner reads the crash text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

from vibesys.dynamic_roles import ORCHESTRATOR

if TYPE_CHECKING:
    from pathlib import Path

_KILLED_CRASH = "HIP out of memory: tried to allocate 20.00 GiB (GPU 0)"
_REPORTED_CRASH = "CUDA error: an illegal memory access was encountered"
# Each crash text holds a run of three backticks, which would end a Markdown fence around it.
_FENCE = " ```python"
_KILLED_VALUE = 7
_REPORTED_VALUE = 8


# The benchmark of a candidate whose server is killed: it prints why to stderr and dies
# with the kernel's out-of-memory status before it writes a result record.
def _killed(message: str, value: int) -> str:
    return (
        "import pathlib, sys\n"
        f'if "VALUE = {value}" in pathlib.Path("queue.py").read_text():\n'
        f"    sys.stderr.write({message!r} + chr(10))\n"
        "    raise SystemExit(137)\n"
    )


# The benchmark of a candidate whose evaluator survives its server's crash and says so.
def _reported(message: str, value: int) -> str:
    return (
        "import json, pathlib, sys\n"
        f'if "VALUE = {value}" in pathlib.Path("queue.py").read_text():\n'
        '    output = sys.argv[sys.argv.index("--vs-output") + 1]\n'
        '    hello = {"kind": "hello", "protocol": 2, "metrics": {"throughput": {"direction": "max"}}}\n'
        f'    error = {{"kind": "error", "message": {message!r}}}\n'
        '    pathlib.Path(output).write_text("".join(json.dumps(r) + chr(10) for r in (hello, error)))\n'
        "    raise SystemExit(139)\n"
    )


def _assert_fenced(prompt: str, message: str) -> None:
    """The crash text stays inside a fence longer than the run of backticks it holds."""
    lines = prompt.splitlines()
    crash = next(i for i, line in enumerate(lines) if message in line)
    fences = [i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {"`"}]
    before = max(i for i in fences if i < crash)
    after = min(i for i in fences if i > crash)
    assert lines[before].strip() == lines[after].strip()
    assert len(lines[before].strip()) > 3


def test_a_crashing_candidate_is_measured_by_its_class_and_the_planner_reads_the_crash(
    tmp_path: Path,
) -> None:
    """Two candidates crash the benchmark, each in the way one class of evaluator leaves.

    A's server is killed with no result record, which might be a fluke: it is measured once
    more. B's evaluator wrote its own error record, which is the candidate's: it is measured
    once. The input is measured by one job. The planner that writes the next workstream reads
    both crash texts.
    """
    loop_input = LoopInput.create(tmp_path)
    killed, reported = _KILLED_CRASH + _FENCE, _REPORTED_CRASH + _FENCE
    benchmark = loop_input.root / "benchmark.py"
    benchmark.write_text(
        _killed(killed, _KILLED_VALUE)
        + _reported(reported, _REPORTED_VALUE)
        + benchmark.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    agents = (
        ScriptedAgents()
        .plan(portfolio(workstream("A")), portfolio(workstream("B")), portfolio(workstream("C")))
        .implement("A", edit_to(_KILLED_VALUE, "A"))
        .judge("A", PASS)
        .implement("B", edit_to(_REPORTED_VALUE, "B"))
        .judge("B", PASS)
        .implement("C", implemented("C", outcome="blocked"))
    )

    run = run_request(
        loop_input.request(max_rounds=3),
        agents,
        slurm_process=loop_input.connector,
        state_stores=loop_input.state_stores,
    )

    assert run.error is None
    assert agents.unscripted == []
    records = CoreRecords(loop_input, run.run_id)
    assert records.attempt("A")["measurements"] == 2
    assert records.attempt("B")["measurements"] == 1
    assert loop_input.sbatch_count() == 1 + 2 + 1
    # The last plan is the first that sees both finished candidates.
    prompt = agents.prompts(ORCHESTRATOR.id)[-1]
    for message in (killed, reported):
        assert message in prompt
        _assert_fenced(prompt, message)
