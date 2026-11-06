"""The accuracy check's wrapper: ``Accuracy`` and ``Fault``, and the request ``bench.py accuracy`` sends.

The run itself is the core's (``vibesys-evaluator accuracy run``); here the
core's ``stream`` is replaced to see the request and to answer with a verdict.
The end-to-end test (``e2e/test_local_loadgen.py``) runs the real one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator._private import core
from vibesys_evaluator.microservices._k8s import deployer

# test-isolation: main() exits; the command is tested through its private entry, with the
# local deploy kind and the core's stream replaced.
from vibesys_evaluator.microservices._private import accuracy as accuracy_check
from vibesys_evaluator.microservices._private.deploy import TaskCommand
from vibesys_evaluator.microservices._private.entry import Task, accuracy
from vibesys_evaluator.microservices._private.local import LocalDeployer

from .vse_support import kv

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys_evaluator.microservices._private.deploy import Deployer

SCRIPT = TaskCommand(python="python3", script="/task/bench.py")
CHECK = ms.Accuracy(
    phases=3,
    rate=50,
    secs=2,
    warmup=0.5,
    arrivals="uniform",
    faults=[ms.Fault.restart_all("kv", after=0), ms.Fault.exec("kv", ["sync"], after=1)],
    oracle=["./kv-oracle", "--quiet"],
)


def _task(check: ms.Accuracy | None = CHECK) -> Task:
    return Task(
        dataset=kv.Data,
        system=kv.SYSTEM,
        workload=kv.WORKLOAD,
        benchmark=kv.benchmark,
        metrics=kv.METRICS,
        validity=vse.Validity(),
        accuracy=check,
    )


def _core(
    monkeypatch: pytest.MonkeyPatch, reply: Callable[[Mapping[str, Any]], dict[str, Any]]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Replace the core's ``stream``; the calls it gets."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def stream(
        group: str,
        verb: str,
        request: Mapping[str, Any] | None = None,
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        calls.append((group, verb, json.loads(json.dumps(request))))
        on_event({"stage": "log", "message": "phase 0 ran"})
        return reply(request or {})

    monkeypatch.setattr(core, "stream", stream)
    return calls


def _run(
    tmp_path: Path, task: Task, *, deployer: Deployer, env: Mapping[str, str] | None = None
) -> int:
    return accuracy(
        task,
        project_root=tmp_path,
        out=tmp_path / "accuracy",
        seed_flag=None,
        deployer=deployer,
        env=env or {"VIBESYS_SAMPLE_SEED": "99"},
        script=SCRIPT,
    )


@pytest.mark.parametrize(("verdict", "code"), [("PASS", 0), ("FAIL", 1), ("UNKNOWN", 2)])
def test_the_exit_status_is_the_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, code: int
) -> None:
    _core(monkeypatch, lambda _request: {"verdict": verdict, "exit_code": code})
    assert _run(tmp_path, _task(), deployer=LocalDeployer(target="http://kv:1")) == code


def test_a_local_system_sends_its_target_and_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _core(monkeypatch, lambda _request: {"verdict": "PASS", "exit_code": 0})
    _run(tmp_path, _task(), deployer=LocalDeployer(target="http://127.0.0.1:9"))
    [(group, verb, request)] = calls
    assert (group, verb) == ("accuracy", "run")
    history = tmp_path / "accuracy" / "history"
    digest = request["local"]["seed"][-1]
    assert request["local"] == {
        "target": "http://127.0.0.1:9",
        "seed": ["python3", "/task/bench.py", "seed", "--dataset-digest", digest],
    }
    assert request["phase_command"] == ["python3", "/task/bench.py", "phase"]
    assert request["sample_seed"] == "99"
    assert request["phases"] == 3
    assert request["load"] == {
        "model": "LOAD_MODEL_OPEN",
        "rate": 50.0,
        "arrivals": "ARRIVALS_UNIFORM",
        "secs": 2.0,
        "warmup_secs": 0.5,
    }
    assert request["faults"] == [
        {"after_phase": 0, "action": "ACCURACY_FAULT_ACTION_RESTART_ALL", "deployment": "kv"},
        {
            "after_phase": 1,
            "action": "ACCURACY_FAULT_ACTION_EXEC",
            "deployment": "kv",
            "argv": ["sync"],
        },
    ]
    assert request["history_dir"] == str(history)
    assert request["out_dir"] == str(tmp_path / "accuracy")
    # A relative oracle is next to the task script, as a relative generator is.
    assert request["oracle"] == ["/task/kv-oracle", "--quiet"]
    # The dataset file is in the history, as the oracle reads it.
    assert json.loads((history / "dataset.json").read_text())["users"]


def test_a_kubernetes_system_sends_the_app_and_its_probe_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, bytes] = {}

    def reply(request: Mapping[str, Any]) -> dict[str, Any]:
        seen["plan"] = Path(request["k8s"]["probe_plan_file"]).read_bytes()
        return {"verdict": "PASS", "exit_code": 0}

    calls = _core(monkeypatch, reply)
    step = tmp_path / "job" / "accuracy"
    env = {"VIBESYS_SAMPLE_SEED": "99", "VIBESYS_ARTIFACTS_DIR": str(step)}
    assert _run(tmp_path, _task(), deployer=deployer(kv.SYSTEM), env=env) == 0
    [(_, _, request)] = calls
    deploy = request["k8s"]
    assert deploy["app"]["name"] == "kv-test"
    assert deploy["task_dir"] == "/task"
    assert deploy["project_root"] == str(tmp_path.resolve())
    # The deploy's logs and the namespace's job label come from the job's artifacts directory.
    assert deploy["artifacts_dir"] == str(step)
    assert "seed" not in deploy  # kv.SYSTEM has no seed function
    assert seen["plan"]  # the ready steps, as the probe pod reads them
    assert "local" not in request


def test_a_core_that_cannot_run_the_check_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def reply(_request: Mapping[str, Any]) -> dict[str, Any]:
        raise core.CoreDomainError("deploy", "the namespace never served", {})

    _core(monkeypatch, reply)
    assert _run(tmp_path, _task(), deployer=LocalDeployer(target="http://kv:1")) == 2
    assert "UNKNOWN: the accuracy run did not complete" in capsys.readouterr().err


def test_a_task_without_an_accuracy_check_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _core(monkeypatch, lambda _request: {"verdict": "PASS", "exit_code": 0})
    assert _run(tmp_path, _task(None), deployer=LocalDeployer(target="http://kv:1")) == 2
    assert calls == []


def test_the_report_goes_into_the_artifacts() -> None:
    env = {"VIBESYS_ARTIFACTS_DIR": "/job/artifacts"}
    assert str(accuracy_check.report_dir(env)) == "/job/artifacts/accuracy"
    assert str(accuracy_check.report_dir({})) == "/vibesys/artifacts/accuracy"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: ms.Fault.restart("kv", after=-1), "phase index"),
        (lambda: ms.Fault.restart("Not_A_Name", after=0), "Deployment's name"),
        (lambda: ms.Fault("exec", "kv", 0), "argv"),
        (lambda: ms.Fault("restart", "kv", 0, ("x",)), "argv"),
        (lambda: ms.Accuracy(phases=0, rate=1, secs=1, oracle=["o"]), "phases"),
        (lambda: ms.Accuracy(phases=1, rate=0, secs=1, oracle=["o"]), "rate"),
        (lambda: ms.Accuracy(phases=1, rate=1, secs=1, warmup=-1, oracle=["o"]), "warmup"),
        (lambda: ms.Accuracy(phases=1, rate=1, secs=1, oracle="o"), "oracle"),
        (
            lambda: ms.Accuracy(
                phases=2, rate=1, secs=1, oracle=["o"], faults=[ms.Fault.restart("kv", after=1)]
            ),
            "between two of the 2 phases",
        ),
    ],
)
def test_a_bad_check_is_refused_when_it_is_made(make: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make()
