"""The driver receives the typed source rather than reconstructing it from prompt text."""

from agentshim import AssistantText

from tests import helpers as h
from tests.fakes import ScriptedDriver, TurnScript
from vs_sidecar.engine import Engine
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.store import MemoryStore


def test_a_turn_keeps_its_typed_job_notification_at_the_driver_boundary() -> None:
    driver = ScriptedDriver([TurnScript([])])
    engine = Engine(h.REF, MemoryStore(), lambda _sink: driver)
    engine.start()
    command = h.start_turn("e1", "t1")
    source = h.message("notification-1")
    source.job_finished.CopyFrom(orch.JobFinished(job_id="j1", outcome=orch.JOB_OUTCOME_SUCCEEDED))
    command.start_turn.message.CopyFrom(source)
    engine.send(command)
    assert engine.run_next()
    assert driver.messages == [source]
    engine.release()


def test_a_steer_keeps_its_typed_job_notification_at_the_driver_boundary() -> None:
    command = h.steer("e2", "t1", "notification-1")
    command.steer.message.job_finished.CopyFrom(
        orch.JobFinished(job_id="j1", outcome=orch.JOB_OUTCOME_SUCCEEDED)
    )
    driver = ScriptedDriver()
    engine = Engine(h.REF, MemoryStore(), lambda _sink: driver)
    driver.turns.append(
        TurnScript([lambda: engine.send(command)], on_steer=[AssistantText("noted")])
    )
    engine.start()
    engine.send(h.start_turn("e1", "t1"))
    assert engine.run_next()
    assert driver.steer_messages == [command.steer.message]
    engine.release()
