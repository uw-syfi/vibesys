"""Wire compatibility with the exact P1 generated Record from 1ffdf1c0b174.

The retained .proto and generated Python fixture are verbatim from that commit.
Load its classes in their own descriptor namespace, using the unchanged shared
leaf contracts. No production module or class is replaced during this test.
"""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_proto.api import (
    Lifecycle,
    Record,
    Running,
    replay,
    validate,
)


def p1_contract() -> ModuleType:
    source = Path(__file__).parent / "fixtures" / "p1_controls" / "record_pb.py.txt"
    # This gives the verbatim generated relative imports their original package.
    # The historical Record and EpochStarted remain separate Python classes.
    loader = SourceFileLoader("vs_proto._generated.vibesys.store.v1.p1_record_fixture", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p1_declaration_amendment_decodes_without_becoming_a_replay_clock() -> None:
    p1 = p1_contract()
    original = p1.Record(
        seq=7,
        at=Timestamp(seconds=1),
        epoch=2,
        clock=3,
        body=Oneof("declaration_amended", p1.EpochStarted(run_id="run-a", epoch=2)),
    )
    validate(original)
    encoded = original.to_binary()
    current = Record.from_binary(encoded)
    assert current.seq == 7
    assert current.epoch == 2
    assert current.clock == 3
    assert current.replay_clock is None
    assert current.body is not None
    assert current.body.field == "declaration_amended"
    assert current.body.value.run_id == "run-a"
    assert current.body.value.epoch == 2
    validate(current)
    assert current.to_binary() == encoded


def test_replay_clock_and_native_body_round_trip_through_p1_generated_record() -> None:
    p1 = p1_contract()
    original = Record(
        seq=7,
        at=Timestamp(seconds=1),
        epoch=2,
        clock=3,
        replay_clock=replay.ReplayClock(allowance=replay.ReplayAllowance(iterations=4)),
        body=Oneof("lifecycle", Lifecycle(state=Oneof("running", Running()))),
    )
    validate(original)
    older = p1.Record.from_binary(original.to_binary())
    validate(older)
    assert older.seq == 7
    assert older.epoch == 2
    assert older.clock == 3
    assert older.body == original.body
    recovered = Record.from_binary(older.to_binary())
    assert recovered == original
    assert (
        next(field.number for field in Record.desc().fields if field.name == "replay_clock") == 21
    )
