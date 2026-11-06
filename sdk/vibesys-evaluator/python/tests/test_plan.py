"""The request stream: what a Python workload's ``generate`` writes for loadgen.

The stream is a pure function of the phase input; ``build`` may change the
model, and later operations see it; every request is encoded as the schema
says; under capture every operation keeps its bodies and is kept, with its
info, before it is written.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

# test-isolation: generation is the phase pod's private step; its output is the schema's file.
from vibesys_evaluator._private.files import read_records, read_requests
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices import (
    Operation,
    Request,
    Workload,
    http,
    post,
    put,
    ref,
)
from vibesys_evaluator.microservices._private.phase import KEPT_FILE, generate

from .vse_support import kv
from .vse_support.fake_loadgen import Capped, whole_frames

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys_evaluator import Rand


def _phase(
    index: int = 0,
    *,
    rate: float = 50.0,
    secs: float = 1.0,
    warmup_secs: float = 1.0,
    capture: bool = False,
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
        capture=capture,
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
    path: Path,
    workload: Workload,
    phase: phase_pb.PhaseInput,
    data: object = None,
    *,
    cap: int = 1 << 16,
) -> list[pb.PlannedOperation]:
    """The stream's first operations: ``generate`` until the stream is closed after ``cap`` bytes."""
    data = kv.Data() if data is None else data
    out = Capped(cap)
    path.mkdir()
    written = generate(workload, data, phase, out=out, state=path)
    (path / "stream.binpb").write_bytes(whole_frames(out.getvalue()))
    header, plan = read_requests(path / "stream.binpb")  # with loadgen's rules (refs)
    assert len(plan) == written
    assert list(header.operation_names) == [op.name for op in workload.operations]
    return plan


def test_the_stream_is_a_function_of_the_phase_input(tmp_path: Path) -> None:
    a = _plan(tmp_path / "a", kv.WORKLOAD, _phase())
    _plan(tmp_path / "b", kv.WORKLOAD, _phase())
    c = _plan(tmp_path / "c", kv.WORKLOAD, _phase(index=1))
    assert (tmp_path / "a/stream.binpb").read_bytes() == (tmp_path / "b/stream.binpb").read_bytes()
    assert a != c


def test_a_longer_stream_starts_with_the_shorter_one(tmp_path: Path) -> None:
    short = _plan(tmp_path / "short", kv.WORKLOAD, _phase(), cap=1 << 12)
    long = _plan(tmp_path / "long", kv.WORKLOAD, _phase(), cap=1 << 16)
    assert len(long) > len(short) > 0
    assert long[: len(short)] == short


def test_a_benchmark_keeps_no_body_and_no_record(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "p", kv.WORKLOAD, _phase())
    assert not any(op.keep_bodies for op in plan)
    assert not (tmp_path / "p" / KEPT_FILE).exists()


def test_capture_keeps_every_operation_before_it_is_written(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "p", kv.WORKLOAD, _phase(index=2, capture=True))
    kept = list(read_records(tmp_path / "p" / KEPT_FILE, phase_pb.KeptOperation))
    assert all(op.keep_bodies for op in plan)
    assert len(kept) >= len(plan)  # the last ones are kept, then not written: the stream closed
    assert [k.index for k in kept] == list(range(len(kept)))
    assert [k.operation for k in kept[: len(plan)]] == [op.operation for op in plan]
    writes = [json.loads(k.info_json) for k in kept if k.operation == 1]
    assert writes
    assert all(w.keys() == {"key", "seed", "phase", "nth"} and w["phase"] == 2 for w in writes)


def test_capture_does_not_change_what_is_sent(tmp_path: Path) -> None:
    plain = _plan(tmp_path / "plain", kv.WORKLOAD, _phase())
    captured = _plan(tmp_path / "captured", kv.WORKLOAD, _phase(capture=True))
    common = min(len(plain), len(captured))  # bodies kept make each operation longer
    assert common > 100
    assert [op.steps for op in plain[:common]] == [op.steps for op in captured[:common]]


def test_the_mix_follows_the_weights(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "p", kv.WORKLOAD, _phase(), cap=1 << 19)
    assert len(plan) > 1000
    writes = [op for op in plan if op.operation == 1]
    assert 0.15 < len(writes) / len(plan) < 0.25  # weight 20 of 100


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


def test_a_model_change_by_build_is_seen_by_the_requests_built_after_it(tmp_path: Path) -> None:
    class Model:
        def __init__(self, _data: object, _rand: Rand) -> None:
            self.counter = 0

    def increment(m: Model, _rand: Rand) -> Request:
        m.counter += 1
        return Request(post("/set", query={"n": str(m.counter)}), info={"n": m.counter})

    workload = Workload(model=Model, operations=[Operation(name="inc", weight=1, build=increment)])
    plan = _plan(tmp_path / "p", workload, _phase())
    sent = [int(str(_parts(_http(op.steps[0]).query[0].value)[0])) for op in plan]
    assert sent == list(range(1, len(plan) + 1))


def test_begin_phase_sees_the_phase_before_the_first_build(tmp_path: Path) -> None:
    class Model:
        def __init__(self, _data: object, _rand: Rand) -> None:
            self.phase = -1

    def begin(m: Model, phase: int) -> None:
        m.phase = phase

    def build(m: Model, _rand: Rand) -> Request:
        return Request(http("DELETE", f"/phase/{m.phase}"))

    workload = Workload(
        model=Model, operations=[Operation(name="op", weight=1, build=build)], begin_phase=begin
    )
    plan = _plan(tmp_path / "p", workload, _phase(index=4))
    first = _http(plan[0].steps[0])
    assert first.method == pb.HttpMethod.DELETE
    assert _parts(first.path) == ["/phase/4"]
