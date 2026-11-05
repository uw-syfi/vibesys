"""The plan: the requests file a phase pod writes before loadgen starts.

Generation is a pure function of the phase input; writes are applied as
pending while it runs; the validation sample is drawn with it; every request
is encoded as the schema says.
"""

from __future__ import annotations

import gc
import json
import weakref
from typing import TYPE_CHECKING

# test-isolation: generation is the phase pod's private step; its output is the schema's file.
from vibesys_evaluator._private.files import read_requests
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices import (
    Call,
    Operation,
    Request,
    Versions,
    Workload,
    post,
    put,
    ref,
)
from vibesys_evaluator.microservices._private.phase import build_model, check, generate, plan_size

from .vse_support import kv

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vibesys_evaluator import Rand


def _phase(
    index: int = 0, *, rate: float = 50.0, secs: float = 1.0, warmup_secs: float = 1.0
) -> phase_pb.PhaseInput:
    """An open-loop phase input."""
    return phase_pb.PhaseInput(
        sample_seed=3,
        phase_index=index,
        model=pb.LoadModel.OPEN,
        rate=rate,
        arrivals=pb.Arrivals.UNIFORM,
        secs=secs,
        warmup_secs=warmup_secs,
    )


def _http(step: pb.Step) -> pb.HttpCall:
    assert step.call is not None
    assert isinstance(step.call.value, pb.HttpCall)
    return step.call.value


def _parts(template: pb.Template | None) -> list[str | pb.Ref]:
    """A template's parts: literals as text, refs as ``Ref``."""
    assert template is not None
    parts: list[str | pb.Ref] = []
    for part in template.parts:
        assert part.part is not None
        parts.append(part.part.value)
    return parts


def _ref(part: str | pb.Ref) -> pb.Ref:
    assert isinstance(part, pb.Ref)
    return part


def _body(call: pb.HttpCall) -> tuple[str, bytes | pb.Template]:
    assert call.body is not None
    assert not isinstance(call.body.value, pb.Form)
    return call.body.field, call.body.value


def _plan(
    path: Path, workload: Workload, phase: phase_pb.PhaseInput, data: object = None
) -> list[pb.PlannedOperation]:
    data = kv.Data() if data is None else data
    generate(workload, build_model(workload, data, phase), phase, path)
    header, plan = read_requests(path)  # as loadgen holds a plan: its count and refs
    assert len(plan) == plan_size(phase)
    assert list(header.operation_names) == [op.name for op in workload.operations]
    return plan


def test_the_plan_is_a_function_of_the_phase_input(tmp_path: Path) -> None:
    a = _plan(tmp_path / "a", kv.WORKLOAD, _phase())
    _plan(tmp_path / "b", kv.WORKLOAD, _phase())
    c = _plan(tmp_path / "c", kv.WORKLOAD, _phase(index=1))
    assert (tmp_path / "a").read_bytes() == (tmp_path / "b").read_bytes()
    assert a != c


def test_plan_sizes_follow_the_load() -> None:
    assert plan_size(_phase(rate=100.0, secs=10.0, warmup_secs=5.0)) == 1630
    assert (
        plan_size(
            phase_pb.PhaseInput(
                model=pb.LoadModel.CLOSED, max_ops_per_sec=200.0, secs=2.0, warmup_secs=1.0
            )
        )
        == 600
    )


def test_the_mix_and_the_sample_follow_the_weights(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "p", kv.WORKLOAD, _phase(rate=1000.0, secs=2.0, warmup_secs=0.0))
    writes = [op for op in plan if op.operation == 1]
    reads = [op for op in plan if op.operation == 0]
    assert 0.15 < len(writes) / len(plan) < 0.25  # weight 20 of 100
    assert all(op.keep_bodies for op in writes)  # writes are always in the sample
    assert 0.4 < sum(op.keep_bodies for op in reads) / len(reads) < 0.6  # sample=0.5
    # a write's exclusive key is the key it puts
    assert all(_parts(_http(op.steps[1]).path) == [f"/kv/{op.exclusive_key}"] for op in writes)


def test_a_template_ref_is_encoded_for_where_it_is_used(tmp_path: Path) -> None:
    def build(_m: object, _rand: Rand) -> Request:
        return Request(
            post("/login", json={"user": 'u"1'}, capture={"token": "/token"}),
            post(
                "/use",
                json={"token": ref("token"), "n": [1, None]},
                headers={"Authorization": "Bearer " + ref("token")},
                query={"q": ref("token")},
            ),
            put("/raw", body=b"\x00", form=None),
            info={"n": 1},
        )

    workload = Workload(
        model=lambda d, _r: d, operations=[Operation(name="op", weight=1, build=build)]
    )
    first = _plan(tmp_path / "p", workload, _phase())[0]
    login, use, raw = (_http(s) for s in first.steps)
    kind, body = _body(login)
    assert kind == "raw"  # no ref: sent as is
    assert isinstance(body, bytes)
    assert json.loads(body) == {"user": 'u"1'}
    assert [(p.name, _parts(p.value)) for p in login.headers] == [
        ("Content-Type", ["application/json"])
    ]
    capture = first.steps[0].captures[0]
    assert capture.source is not None
    assert (capture.name, capture.source.field, capture.source.value) == (
        "token",
        "json_pointer",
        "/token",
    )
    kind, text = _body(use)
    assert kind == "text"
    assert isinstance(text, pb.Template)
    parts = _parts(text)
    assert parts[0] == '{"token":"'
    assert _ref(parts[1]).encoding == pb.RefEncoding.JSON_STRING
    assert parts[2] == '","n":[1,null]}'
    header = next(p for p in use.headers if p.name == "Authorization")
    literal, token = _parts(header.value)
    assert literal == "Bearer "
    assert _ref(token).encoding == pb.RefEncoding.RAW
    assert _ref(_parts(use.query[0].value)[0]).encoding == pb.RefEncoding.RAW
    assert raw.method == pb.HttpMethod.PUT
    assert _body(raw) == ("raw", b"\x00")


def test_a_pending_write_is_seen_by_the_requests_built_after_it(tmp_path: Path) -> None:
    class Model:
        def __init__(self, _data: object, _rand: Rand) -> None:
            self.counter: Versions[str, int] = Versions()
            self.pending: list[Call] = []

    def increment(m: Model, _rand: Rand) -> Request:
        n = m.counter.latest("c", 0) + 1
        return Request(post("/set", query={"n": str(n)}), info={"n": n}, exclusive="c")

    def apply(m: Model, call: Call) -> None:
        m.pending.append(call)
        m.counter.write("c", call.info["n"], call)

    workload = Workload(
        model=Model,
        operations=[
            Operation(name="inc", weight=1, build=increment, apply=apply, check=lambda _m, _c: None)
        ],
    )
    plan = _plan(tmp_path / "p", workload, _phase())
    sent = [int(str(_parts(_http(op.steps[0]).query[0].value)[0])) for op in plan]
    assert sent == list(range(1, len(plan) + 1))


def test_the_check_keeps_only_the_results_of_writes_and_sampled_operations(tmp_path: Path) -> None:
    # A large plan's results do not fit in the phase pod as objects: the check streams them and
    # keeps only those it applies or checks. Every result it drops is freed while it runs.
    data = kv.Data()
    phase = _phase(rate=400.0)
    model = build_model(kv.WORKLOAD, data, phase)
    plan = generate(kv.WORKLOAD, model, phase, tmp_path / "requests.binpb")
    alive: list[weakref.ref[pb.OperationResult]] = []
    kept_alive: list[int] = []

    def results() -> Iterator[pb.OperationResult]:
        for index in range(len(plan.operations)):
            result = pb.OperationResult(
                index=index,
                start_unix_nanos=1 + index,
                end_unix_nanos=2 + index,
                steps=[pb.StepResult(status=200), pb.StepResult(status=200)],
            )
            alive.append(weakref.ref(result))
            yield result
            del result
        gc.collect()
        kept_alive.append(sum(ref() is not None for ref in alive))

    summary = pb.PhaseSummary(window_start_unix_nanos=0, window_end_unix_nanos=10**12)
    output = check(kv.WORKLOAD, data, phase, plan, summary, results())
    assert kept_alive == [len(plan.infos)]
    assert len(plan.infos) < len(plan.operations)
    assert len(output.writes) == sum(1 for i in plan.operations if i == 1)
