"""The JSONSchemaBench task of the Llama 3.1 8B MLX example: what it asks, how it judges, how it runs.

The task is written for vibesys-cli (schema 0.2, on the host, as the Metal backend needs). Its
workload module is pure, so these tests drive its public functions; the server launch and the
benchmark are real-system code that the example's own live run covers.
"""

from __future__ import annotations

import ast
import json
import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

PROJECT = Path(__file__).parents[2] / "examples/model-serving/Llama-3.1-8B-Instruct-MLX-8bit"
TASK = PROJECT / ".vibesys/tasks/default"
# run_path loads the task's own module as its public API, without importing it by name.
WORKLOAD = SimpleNamespace(**runpy.run_path(str(TASK / "workload.py")))

unique_ids = st.text(st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=12)
pools = st.dictionaries(unique_ids, st.just({"type": "string"}), min_size=1, max_size=40).map(
    lambda schemas: list(schemas.items())
)


def manifest() -> dict[str, Any]:
    return tomllib.loads((TASK / "vibesys.input.toml").read_text())


# --- the manifest ---------------------------------------------------------------------------


def test_the_task_runs_on_the_host_with_one_script_for_every_mode() -> None:
    parsed = manifest()
    assert parsed["version"] == "0.2"
    assert parsed["backends"] == ["metal"]
    # Metal needs the host: a Dockerfile or a [candidate] image would make vibesys-cli refuse it.
    assert "candidate" not in parsed
    assert not (TASK / "Dockerfile").exists()
    modes = {"accuracy": "check", "benchmark": "benchmark"}
    for section, mode in modes.items():
        command = parsed[section]["command"]
        assert command[-2:] == ["${PROJECT_ROOT}/.vibesys/tasks/default/bench.py", mode]
    assert parsed["benchmark"]["result_protocol"] == 2


def test_the_headline_metric_is_a_metric_the_script_declares() -> None:
    objectives = tomllib.loads((TASK / "objectives.toml").read_text())["objective"]
    assert objectives == [{"name": "latency_ms.p50", "direction": "min"}]
    script = ast.parse((TASK / "bench.py").read_text())
    declared = {
        key.value
        for node in ast.walk(script)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "METRICS" for t in node.targets)
        and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant)
    }
    assert objectives[0]["name"] in declared


def test_the_objective_documents_the_launch_contract_the_script_uses() -> None:
    launch = runpy.run_path(str(TASK / "launch.py"))
    command = launch["command"](Path("/work"), Path("/models/llama"), 8123)
    assert command[:5] == ["uv", "run", "--project", "/work", "python"]
    assert command[5:7] == ["-m", "engine.server"]
    flags = dict(zip(command[7::2], command[8::2], strict=True))
    assert flags == {"--model-dir": "/models/llama", "--host": "127.0.0.1", "--port": "8123"}
    objective = (TASK / "OBJECTIVE.md").read_text()
    assert "python -m engine.server" in objective
    assert all(flag in objective for flag in flags)
    assert (PROJECT / "engine/server.py").is_file()


# --- which schemas are asked ----------------------------------------------------------------


@given(pool=pools, small=st.integers(0, 45), large=st.integers(0, 45), data=st.data())
def test_a_smaller_selection_is_a_prefix_of_a_larger_one_whatever_the_pool_order(
    pool: list[tuple[str, dict[str, object]]], small: int, large: int, data: st.DataObject
) -> None:
    small, large = sorted((small, large))
    chosen = WORKLOAD.select(pool, large)
    assert WORKLOAD.select(pool, small) == chosen[:small]
    assert WORKLOAD.select(data.draw(st.permutations(pool)), large) == chosen
    assert len(chosen) == min(large, len(pool))


# --- what a request says --------------------------------------------------------------------

SCHEMAS = st.recursive(
    st.sampled_from(
        [{"type": "string"}, {"type": "integer"}, {"type": ["null", "string"]}, {"type": "boolean"}]
    ),
    lambda inner: st.one_of(
        st.builds(lambda item: {"type": "array", "items": item}, inner),
        st.dictionaries(st.sampled_from("abcd"), inner, min_size=1, max_size=3).map(
            lambda props: {"type": "object", "properties": props, "required": sorted(props)}
        ),
    ),
    max_leaves=6,
)


@given(schema=SCHEMAS, unique_id=unique_ids)
def test_a_request_carries_a_sentinel_exactly_when_the_schema_can_hold_a_string(
    schema: dict[str, object], unique_id: str
) -> None:
    request = WORKLOAD.case(unique_id, schema)
    assert (request.sentinel is not None) == WORKLOAD.can_hold_sentinel(schema)
    if request.sentinel is None:
        assert "exact token" not in request.prompt
    else:
        assert request.sentinel in request.prompt
        assert request.sentinel.startswith("sx-")
    assert json.dumps(schema, indent=2) in request.prompt
    assert request == WORKLOAD.case(unique_id, schema)


# --- what an answer is worth ----------------------------------------------------------------


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


@given(unique_id=unique_ids, answer=st.text())
def test_an_answer_is_valid_when_it_is_json_of_the_schema_and_holds_the_sentinel_when_it_names_it(
    unique_id: str, answer: str
) -> None:
    request = WORKLOAD.case(unique_id, {"type": "string"})
    text = json.dumps(answer)
    verdict = WORKLOAD.judge(request, text)
    assert verdict.valid
    assert verdict.sentinel_ok == (request.sentinel in text)
    assert WORKLOAD.judge(request, json.dumps(request.sentinel)).sentinel_ok


@given(unique_id=unique_ids, value=st.one_of(st.integers(), st.booleans(), st.lists(st.integers())))
def test_a_template_that_ignores_the_prompt_fails_the_sentinel_gate(
    unique_id: str, value: object
) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    request = WORKLOAD.case(unique_id, schema)
    verdict = WORKLOAD.judge(request, json.dumps({"x": "fixed", "y": value}))
    assert verdict.valid
    assert verdict.sentinel_ok is False


@given(unique_id=unique_ids, text=st.text().filter(lambda t: not _is_json(t)))
def test_an_answer_that_is_not_json_is_not_valid(unique_id: str, text: str) -> None:
    verdict = WORKLOAD.judge(WORKLOAD.case(unique_id, {"type": "string"}), text)
    assert not verdict.valid
    assert verdict.error is not None
    assert verdict.error.startswith("parse failed")


@given(answer=st.one_of(st.integers(), st.booleans(), st.none(), st.lists(st.integers())))
def test_an_answer_of_another_type_than_the_schema_is_not_valid(answer: object) -> None:
    verdict = WORKLOAD.judge(WORKLOAD.case("a", {"type": "string"}), json.dumps(answer))
    assert not verdict.valid
    assert verdict.error is not None
    assert verdict.error.startswith("schema validation failed")


def verdicts(valid: int, invalid: int, held: int, missed: int) -> list[Any]:
    """Answers that are valid or not, and (for the valid ones) hold the sentinel or not."""
    ok = [WORKLOAD.Verdict(valid=True, error=None, sentinel_ok=True)] * held
    ok += [WORKLOAD.Verdict(valid=True, error=None, sentinel_ok=False)] * missed
    ok += [WORKLOAD.Verdict(valid=True, error=None, sentinel_ok=None)] * (valid - held - missed)
    return ok + [WORKLOAD.Verdict(valid=False, error="bad", sentinel_ok=None)] * invalid


@given(valid=st.integers(1, 60), invalid=st.integers(0, 60), held=st.integers(0, 60))
def test_the_gate_passes_exactly_the_answers_at_both_thresholds(
    valid: int, invalid: int, held: int
) -> None:
    held = min(held, valid)
    answers = verdicts(valid, invalid, held, valid - held)
    reason = WORKLOAD.gate(answers)
    meets_validity = valid / (valid + invalid) >= WORKLOAD.MIN_VALID_RATE
    meets_sentinel = held / valid >= WORKLOAD.MIN_SENTINEL_RATE
    assert (reason is None) == (meets_validity and meets_sentinel)


@given(valid=st.integers(1, 60), invalid=st.integers(0, 60), extra=st.integers(1, 10))
def test_more_invalid_answers_never_turn_a_failing_gate_into_a_pass(
    valid: int, invalid: int, extra: int
) -> None:
    before = WORKLOAD.gate(verdicts(valid, invalid, valid, 0))
    after = WORKLOAD.gate(verdicts(valid, invalid + extra, valid, 0))
    if before is not None:
        assert after is not None


def test_a_schema_that_is_not_a_json_schema_is_not_asked() -> None:
    assert WORKLOAD.is_well_formed({"type": "string"})
    assert not WORKLOAD.is_well_formed({"type": 12})
