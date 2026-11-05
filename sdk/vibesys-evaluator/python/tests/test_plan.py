"""The plan: the requests file a phase pod writes before loadgen starts.

Generation is a pure function of the phase input; writes are applied as
pending while it runs; the validation sample is drawn with it; every request
is encoded as the schema says.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys_evaluator import Call, Rand, Versions

# test-isolation: generation is the phase pod's private step; its output is the schema's file.
from vibesys_evaluator._private.files import DelimitedReader
from vibesys_evaluator._private.phase import build_model, generate, plan_size
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.service import Operation, Request, Workload, post, put, ref

from .support import kv

if TYPE_CHECKING:
    from pathlib import Path


def _phase(index: int = 0, **load: object) -> phase_pb.PhaseInput:
    defaults: dict[str, object] = {
        "sample_seed": 3,
        "phase_index": index,
        "model": pb.LoadModel.OPEN,
        "rate": 50.0,
        "arrivals": pb.Arrivals.UNIFORM,
        "secs": 1.0,
        "warmup_secs": 1.0,
    }
    return phase_pb.PhaseInput(**{**defaults, **load})  # type: ignore[arg-type]


def _plan(
    path: Path, workload: Workload, phase: phase_pb.PhaseInput, data: object = None
) -> list[pb.PlannedOperation]:
    data = kv.Data() if data is None else data
    generate(workload, build_model(workload, data, phase), phase, path)
    reader = DelimitedReader(path, pb.FileKind.REQUESTS)
    header = reader.one(pb.RequestsHeader)
    plan = list(reader.rest(pb.PlannedOperation))
    assert header.count == len(plan) == plan_size(phase)
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
            _phase(model=pb.LoadModel.CLOSED, max_ops_per_sec=200.0, secs=2.0, warmup_secs=1.0)
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
    assert all(
        op.exclusive_key
        == f"k{json.loads(op.steps[1].call.value.path.parts[0].part.value.split('k', 2)[-1] and '0')}"
        or op.exclusive_key.startswith("k")
        for op in writes
    )


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
    login, use, raw = (s.call.value for s in first.steps)
    assert login.body.field == "raw"  # no ref: sent as is
    assert json.loads(login.body.value) == {"user": 'u"1'}
    assert [(p.name, p.value.parts[0].part.value) for p in login.headers] == [
        ("Content-Type", "application/json")
    ]
    capture = first.steps[0].captures[0]
    assert (capture.name, capture.source.field, capture.source.value) == (
        "token",
        "json_pointer",
        "/token",
    )
    text = use.body.value
    assert use.body.field == "text"
    kinds = [(p.part.field, p.part.value) for p in text.parts]
    assert kinds[0] == ("literal", '{"token":"')
    assert kinds[1][0] == "ref"
    assert kinds[1][1].encoding == pb.RefEncoding.JSON_STRING
    assert kinds[2] == ("literal", '","n":[1,null]}')
    header = next(p for p in use.headers if p.name == "Authorization")
    assert [p.part.field for p in header.value.parts] == ["literal", "ref"]
    assert header.value.parts[1].part.value.encoding == pb.RefEncoding.RAW
    assert use.query[0].value.parts[0].part.value.encoding == pb.RefEncoding.RAW
    assert raw.method == pb.HttpMethod.PUT
    assert raw.body.value == b"\x00"


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
    sent = [int(op.steps[0].call.value.query[0].value.parts[0].part.value) for op in plan]
    assert sent == list(range(1, len(plan) + 1))
