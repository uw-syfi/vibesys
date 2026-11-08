"""The JSONSchemaBench task of the Llama 3.1 8B MLX example: what it asks, how it judges, how it runs.

The task is written for vibesys-cli (schema 0.2, on the host, as the Metal backend needs). Its
workload module is pure and its client takes the HTTP connection as an argument, so these tests
drive their public functions, the client over an in-memory fake server; the server launch and the
benchmark are real-system code that the example's own live run covers.
"""

from __future__ import annotations

import ast
import json
import re
import runpy
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PROJECT = Path(__file__).parents[2] / "examples/model-serving/Llama-3.1-8B-Instruct-MLX-8bit"
TASK = PROJECT / ".vibesys/tasks/default"
# run_path loads the task's own modules as their public API, without importing them by name.
WORKLOAD = SimpleNamespace(**runpy.run_path(str(TASK / "workload.py")))
CLIENT = SimpleNamespace(**runpy.run_path(str(TASK / "client.py")))


class VerdictLike(Protocol):
    """The fields of the workload's Verdict that these tests read."""

    outcome: object
    error: str | None
    sentinel_ok: bool | None

    @property
    def valid(self) -> bool: ...


class ReplyLike(Protocol):
    """The fields of the client's Reply that these tests read."""

    text: str
    chunks: int
    error: str | None


unique_ids = st.text(st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=12)
sentinels = st.from_regex(r"sx-[a-z0-9]{8}", fullmatch=True)
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


def test_no_section_of_the_manifest_runs_the_held_out_split() -> None:
    parsed = manifest()
    for section in ("accuracy", "benchmark"):
        assert "holdout" not in " ".join(parsed[section]["command"])
    assert "holdout" in (TASK / "bench.py").read_text()


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


def fixed(sentinel: str = "sx-00000000") -> Callable[[], str]:
    return lambda: sentinel


def ids(cases: list[Any]) -> list[str]:
    return [c.unique_id for c in cases]


@given(pool=pools, start=st.integers(0, 45), count=st.integers(0, 45), data=st.data())
def test_a_selection_is_a_window_of_one_fixed_order_whatever_the_pool_order(
    pool: list[tuple[str, dict[str, object]]], start: int, count: int, data: st.DataObject
) -> None:
    everything = ids(WORKLOAD.select(pool, WORKLOAD.Ranks(0, len(pool)), fixed()))
    window = WORKLOAD.select(pool, WORKLOAD.Ranks(start, count), fixed())
    assert ids(window) == everything[start : start + count]
    shuffled = data.draw(st.permutations(pool))
    assert ids(WORKLOAD.select(shuffled, WORKLOAD.Ranks(start, count), fixed())) == ids(window)


@given(pool=st.dictionaries(unique_ids, st.just({"type": "string"}), min_size=70, max_size=120))
def test_the_check_the_benchmark_and_the_warm_up_ask_different_schemas(
    pool: dict[str, dict[str, object]],
) -> None:
    asked = [
        ids(WORKLOAD.select(list(pool.items()), ranks, fixed()))
        for ranks in (WORKLOAD.BENCHMARK, WORKLOAD.CHECK, WORKLOAD.WARM_UP)
    ]
    assert [len(a) for a in asked] == [WORKLOAD.BENCHMARK.count, WORKLOAD.CHECK.count, 1]
    everyone = [uid for group in asked for uid in group]
    assert len(set(everyone)) == len(everyone)


def test_the_splits_name_the_datasets_own_files() -> None:
    assert WORKLOAD.Split.VAL.file == "data/val-00000-of-00001.parquet"
    assert WORKLOAD.Split.TEST.file == "data/test-00000-of-00001.parquet"
    assert WORKLOAD.Split.TRAIN.file == "data/train-00000-of-00001.parquet"
    assert WORKLOAD.HOLDOUT_WARM_UP.start >= WORKLOAD.HOLDOUT.start + WORKLOAD.HOLDOUT.count


@given(
    number=st.integers(0, 10**6),
    name=st.from_regex(r"[a-z]+(_[a-z]+)*", fullmatch=True),
    digest=st.from_regex(r"[0-9a-f]{8}", fullmatch=True),
)
def test_a_schema_is_traced_to_its_subset_by_the_shape_of_its_id(
    number: int, name: str, digest: str
) -> None:
    assert WORKLOAD.source_of(f"o{number}") == "github"
    assert WORKLOAD.source_of(f"kb_{number}_Normalized") == "kubernetes"
    assert WORKLOAD.source_of(f"sp_{number}_Normalized") == "snowplow"
    assert WORKLOAD.source_of(f"wp_{number}_Normalized") == "washingtonpost"
    assert WORKLOAD.source_of(f"{name}_{digest}") == "glaive"
    assert WORKLOAD.source_of(f"{name}-{number}") == "store"


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


@given(schema=SCHEMAS, unique_id=unique_ids, sentinel=sentinels)
def test_a_request_carries_a_sentinel_exactly_when_the_schema_can_hold_a_string(
    schema: dict[str, object], unique_id: str, sentinel: str
) -> None:
    request = WORKLOAD.case(unique_id, schema, sentinel)
    assert (request.sentinel is not None) == WORKLOAD.can_hold_sentinel(schema)
    if request.sentinel is None:
        assert "exact token" not in request.prompt
    else:
        assert request.sentinel in request.prompt
        assert request.sentinel == sentinel
    assert json.dumps(schema, indent=2) in request.prompt
    assert request == WORKLOAD.case(unique_id, schema, sentinel)
    body = WORKLOAD.request_body(request)
    assert body["prompt"] == request.prompt
    assert body["max_tokens"] == WORKLOAD.MAX_TOKENS
    assert body["response_format"]["json_schema"]["schema"] == schema


@given(schema=SCHEMAS, unique_id=unique_ids, first=sentinels, second=sentinels)
def test_two_runs_ask_the_same_schema_in_prompts_that_differ_only_in_the_sentinel(
    schema: dict[str, object], unique_id: str, first: str, second: str
) -> None:
    one = WORKLOAD.case(unique_id, schema, first)
    two = WORKLOAD.case(unique_id, schema, second)
    assert one.schema == two.schema
    if one.sentinel is None:
        assert one.prompt == two.prompt
    else:
        assert one.prompt.replace(first, "SENTINEL") == two.prompt.replace(second, "SENTINEL")


@given(pool=pools)
def test_a_selection_draws_a_new_sentinel_for_every_request_at_every_run(
    pool: list[tuple[str, dict[str, object]]],
) -> None:
    ranks = WORKLOAD.Ranks(0, len(pool))
    runs = [WORKLOAD.select(pool, ranks) for _ in range(2)]
    assert ids(runs[0]) == ids(runs[1])
    drawn = [c.sentinel for run in runs for c in run]
    assert all(re.fullmatch(r"sx-[a-z0-9]{8}", d) for d in drawn)
    # 36**8 possibilities per draw: a repeat among a few dozen draws is not going to happen.
    assert len(set(drawn)) == len(drawn)


# --- what an answer is worth ----------------------------------------------------------------

LIMIT = WORKLOAD.TRUNCATION_MIN_TOKENS
SENTINEL = "sx-0a1b2c3d"
OUTCOMES = list(WORKLOAD.Outcome)
tokens_counted = st.integers(0, WORKLOAD.MAX_TOKENS + 64)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


@given(unique_id=unique_ids, answer=st.text(), tokens=tokens_counted)
def test_an_answer_is_valid_when_it_is_json_of_the_schema_whatever_its_length(
    unique_id: str, answer: str, tokens: int
) -> None:
    request = WORKLOAD.case(unique_id, {"type": "string"}, SENTINEL)
    text = json.dumps(answer)
    verdict = WORKLOAD.judge(request, text, tokens)
    assert verdict.outcome is WORKLOAD.Outcome.VALID
    assert verdict.valid
    assert verdict.sentinel_ok == (request.sentinel in text)
    assert WORKLOAD.judge(request, json.dumps(request.sentinel), tokens).sentinel_ok


@given(unique_id=unique_ids, value=st.one_of(st.integers(), st.booleans(), st.lists(st.integers())))
def test_a_template_that_ignores_the_prompt_fails_the_sentinel_gate(
    unique_id: str, value: object
) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    request = WORKLOAD.case(unique_id, schema, SENTINEL)
    verdict = WORKLOAD.judge(request, json.dumps({"x": "fixed", "y": value}), 10)
    assert verdict.valid
    assert verdict.sentinel_ok is False


@given(
    unique_id=unique_ids, text=st.text().filter(lambda t: not _is_json(t)), tokens=tokens_counted
)
def test_an_answer_that_is_not_json_is_not_valid(unique_id: str, text: str, tokens: int) -> None:
    verdict = WORKLOAD.judge(WORKLOAD.case(unique_id, {"type": "string"}, SENTINEL), text, tokens)
    assert not verdict.valid
    assert verdict.error is not None
    assert verdict.error.startswith("parse failed")


@given(
    answer=st.one_of(st.integers(), st.booleans(), st.none(), st.lists(st.integers())),
    tokens=tokens_counted,
)
def test_an_answer_of_another_type_than_the_schema_is_not_valid(
    answer: object, tokens: int
) -> None:
    verdict = WORKLOAD.judge(
        WORKLOAD.case("a", {"type": "string"}, SENTINEL), json.dumps(answer), tokens
    )
    assert not verdict.valid
    assert verdict.error is not None
    assert verdict.error.startswith("schema validation failed")


@given(unique_id=unique_ids, text=st.text(), tokens=tokens_counted)
def test_an_invalid_answer_is_cut_off_exactly_when_the_judges_own_count_reaches_the_limit(
    unique_id: str, text: str, tokens: int
) -> None:
    request = WORKLOAD.case(unique_id, {"type": "integer"}, SENTINEL)
    verdict = WORKLOAD.judge(request, text, tokens)
    if verdict.valid:
        assert verdict.outcome is WORKLOAD.Outcome.VALID
    elif tokens >= LIMIT:
        assert verdict.outcome is WORKLOAD.Outcome.CUT_OFF
    else:
        assert verdict.outcome is WORKLOAD.Outcome.INVALID


# --- the gate --------------------------------------------------------------------------------


def verdict_of(outcome: object, *, sentinel_ok: bool | None) -> VerdictLike:
    error = None if outcome is WORKLOAD.Outcome.VALID else "bad"
    return WORKLOAD.Verdict(outcome=outcome, error=error, sentinel_ok=sentinel_ok)


verdict_sets = st.lists(
    st.builds(
        verdict_of, st.sampled_from(OUTCOMES), sentinel_ok=st.sampled_from([None, True, False])
    ),
    min_size=1,
    max_size=60,
)


def counted(answers: list[VerdictLike], outcome: object) -> int:
    return sum(a.outcome is outcome for a in answers)


@given(answers=verdict_sets)
def test_the_gate_never_passes_an_answer_that_ended_invalid(answers: list[VerdictLike]) -> None:
    reason = WORKLOAD.gate(answers)
    if counted(answers, WORKLOAD.Outcome.INVALID):
        assert reason is not None


@given(answers=verdict_sets, extra=st.integers(1, 5))
def test_an_answer_that_ended_invalid_fails_the_gate_however_many_are_right(
    answers: list[VerdictLike], extra: int
) -> None:
    right = [verdict_of(WORKLOAD.Outcome.VALID, sentinel_ok=True)] * (len(answers) * 20)
    reason = WORKLOAD.gate(
        [*right, *answers, verdict_of(WORKLOAD.Outcome.INVALID, sentinel_ok=None)] * extra
    )
    assert reason is not None
    assert "ended but are not valid" in reason


@given(
    valid=st.integers(1, 80),
    cut_off=st.integers(0, 20),
    held=st.integers(0, 80),
)
def test_verified_cut_offs_pass_up_to_the_stated_fraction_and_no_further(
    valid: int, cut_off: int, held: int
) -> None:
    held = min(held, valid)
    answers = [verdict_of(WORKLOAD.Outcome.VALID, sentinel_ok=True)] * valid
    answers += [verdict_of(WORKLOAD.Outcome.CUT_OFF, sentinel_ok=None)] * cut_off
    reason = WORKLOAD.gate(answers)
    assert (reason is None) == (cut_off / (valid + cut_off) <= WORKLOAD.MAX_TRUNCATED_FRACTION)
    if reason is not None:
        assert "cut off" in reason


@given(answers=verdict_sets, index=st.integers(0, 59))
def test_one_more_cut_off_in_place_of_a_right_answer_never_turns_a_failing_gate_into_a_pass(
    answers: list[VerdictLike], index: int
) -> None:
    index %= len(answers)
    worse = list(answers)
    if worse[index].outcome is WORKLOAD.Outcome.VALID:
        worse[index] = verdict_of(WORKLOAD.Outcome.CUT_OFF, sentinel_ok=None)
        if WORKLOAD.gate(answers) is not None:
            assert WORKLOAD.gate(worse) is not None


@given(
    valid=st.integers(1, 60),
    held=st.integers(0, 60),
    cut_off=st.integers(0, 2),
    cut_off_sentinel=st.sampled_from([None, True, False]),
)
def test_the_sentinel_floor_counts_the_right_answers_and_not_the_cut_off_ones(
    *, valid: int, held: int, cut_off: int, cut_off_sentinel: bool | None
) -> None:
    held = min(held, valid)
    answers = [verdict_of(WORKLOAD.Outcome.VALID, sentinel_ok=True)] * held
    answers += [verdict_of(WORKLOAD.Outcome.VALID, sentinel_ok=False)] * (valid - held)
    answers += [verdict_of(WORKLOAD.Outcome.CUT_OFF, sentinel_ok=cut_off_sentinel)] * cut_off
    reason = WORKLOAD.gate(answers)
    if cut_off / len(answers) <= WORKLOAD.MAX_TRUNCATED_FRACTION:
        assert (reason is None) == (held / valid >= WORKLOAD.MIN_SENTINEL_RATE)


def test_no_answers_do_not_pass() -> None:
    assert WORKLOAD.gate([]) is not None


@given(
    answers=st.lists(
        st.just(verdict_of(WORKLOAD.Outcome.VALID, sentinel_ok=None)), min_size=1, max_size=40
    )
)
def test_answers_to_schemas_that_cannot_hold_a_sentinel_are_held_to_no_sentinel_floor(
    answers: list[VerdictLike],
) -> None:
    assert WORKLOAD.gate(answers) is None


# --- the server's claim of truncation is never trusted ----------------------------------------


class FakeResponse:
    """What an httpx streaming response gives the client, with the bytes the test chose."""

    def __init__(self, status: int, content_type: str, lines: list[str]) -> None:
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.text = "\n".join(lines)
        self._lines = lines

    def read(self) -> bytes:
        return self.text.encode()

    def iter_lines(self) -> Iterator[str]:
        return iter(self._lines)


class FakeServer:
    """The connection to a candidate server that answers every request with one canned response."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    @contextmanager
    def stream(self, method: str, url: str, json: object) -> Iterator[FakeResponse]:
        assert method == "POST"
        assert url.endswith("/v1/completions")
        assert json is not None
        yield self._response


def event_stream(pieces: list[str], finish_reason: object) -> FakeResponse:
    events = [
        "data: "
        + json.dumps({"choices": [{"index": 0, "text": piece, "finish_reason": finish_reason}]})
        for piece in pieces
    ]
    return FakeResponse(200, "text/event-stream", [*events, "data: [DONE]"])


def reply_of(response: FakeResponse) -> ReplyLike:
    return CLIENT.stream_completion(FakeServer(response), "http://server", {"prompt": "p"}, 30.0)


finish_reasons = st.one_of(
    st.none(), st.sampled_from(["stop", "length", "content_filter"]), st.text(), st.integers()
)
nonempty = st.text(min_size=1, max_size=20)


@given(pieces=st.lists(nonempty, max_size=30), claimed=finish_reasons, other=finish_reasons)
def test_what_the_server_says_about_why_it_stopped_does_not_change_the_reply(
    pieces: list[str], claimed: object, other: object
) -> None:
    one, two = reply_of(event_stream(pieces, claimed)), reply_of(event_stream(pieces, other))
    assert one.text == two.text == "".join(pieces)
    assert one.chunks == two.chunks == len(pieces)
    assert one.error is None


@given(
    text=st.text(min_size=1, max_size=40).filter(lambda t: not _is_json(t)), unique_id=unique_ids
)
def test_a_short_answer_a_server_claims_was_cut_off_ended_invalid_and_fails_the_gate(
    text: str, unique_id: str
) -> None:
    reply = reply_of(event_stream([text], "length"))
    request = WORKLOAD.case(unique_id, {"type": "string"}, SENTINEL)
    judge_count = len(reply.text.split())  # the judge's own count, far below the limit
    verdict = WORKLOAD.judge(request, reply.text, judge_count)
    assert verdict.outcome is WORKLOAD.Outcome.INVALID
    assert WORKLOAD.gate([verdict]) is not None


@given(
    lines=st.lists(
        st.one_of(
            st.text(),
            st.builds(
                lambda v: "data: " + json.dumps(v),
                st.recursive(
                    st.one_of(st.none(), st.booleans(), st.integers(), st.text()),
                    lambda inner: st.one_of(
                        st.lists(inner, max_size=3),
                        st.dictionaries(st.text(max_size=6), inner, max_size=3),
                    ),
                    max_leaves=8,
                ),
            ),
        ),
        max_size=12,
    )
)
def test_a_hostile_event_stream_never_crashes_the_client(lines: list[str]) -> None:
    reply = reply_of(FakeResponse(200, "text/event-stream", lines))
    assert reply.error is not None or isinstance(reply.text, str)


@given(status=st.integers(300, 599), body=st.text(max_size=50))
def test_a_failed_request_is_a_reply_with_an_error_and_no_answer(status: int, body: str) -> None:
    reply = reply_of(FakeResponse(status, "application/json", [body]))
    assert reply.error is not None
    assert reply.error.startswith(f"HTTP {status}")
    assert reply.text == ""


def test_no_judge_code_reads_the_servers_finish_reason() -> None:
    """The candidate is untrusted: its `finish_reason` appears in no code of the task, only in prose."""
    for name in ("workload.py", "client.py", "bench.py", "launch.py"):
        tree = ast.parse((TASK / name).read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        used = [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Constant)
                and id(node) not in docstrings
                and "finish_reason" in str(node.value)
            )
            or (isinstance(node, ast.Name) and "finish_reason" in node.id)
            or (isinstance(node, ast.Attribute) and "finish_reason" in node.attr)
            or (isinstance(node, ast.arg) and "finish_reason" in node.arg)
        ]
        assert not used, f"{name} reads finish_reason"


def test_a_schema_that_is_not_a_json_schema_is_not_asked() -> None:
    assert WORKLOAD.is_well_formed({"type": "string"})
    assert not WORKLOAD.is_well_formed({"type": 12})
