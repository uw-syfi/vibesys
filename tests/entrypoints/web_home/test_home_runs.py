from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.web_home.support import make_project, project_key
from tests.support.run_execution import run_execution_record

from entrypoints.web_home import runs
from entrypoints.web_home.contract import StartRun
from entrypoints.web_home.runs import render_run_config
from vibesys.api import Config
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.entrypoints.web_home.support import Home

FAKE = Path(__file__).with_name("fake_run_server.py")
START = {
    "task": "bench",
    "outer_loop": "plain",
    "budget": 4,
    "compute_backend": "cpu",
    "provider": "codex",
    "model": "gpt-5.5",
    "roles": {"implementer": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}},
}


@pytest.fixture
def runs_home(home: Home) -> Iterator[Home]:
    home.config.run_server_argv = (sys.executable, str(FAKE))
    try:
        yield home
    finally:
        for launch in home.config.launches.values():
            launch.process.terminate()
            launch.process.wait(timeout=10)


def _project(home: Home) -> tuple[str, Path]:
    root = make_project(home.workspace / "proj")
    return project_key(home, root), root


def _argv(home: Home, name: str = "live") -> list[str]:
    gateways = home.config.state_home / "web" / "gateways"
    return json.loads(next(gateways.glob(f"*/{name}.argv.json")).read_text())


def test_start_returns_the_gateway_and_passes_the_launch_contract(runs_home: Home) -> None:
    key, root = _project(runs_home)

    reply = runs_home.post(f"/api/projects/{key}/runs", START).json()

    argv = _argv(runs_home)
    assert reply["gateway"]["state"] == "starting"
    assert reply["gateway"]["websocket_url"].startswith("ws://127.0.0.1:")
    assert argv[:3] == ["--web", "--detach", "--web-port"]
    assert ["--web-origin", runs_home.config.origin] == argv[argv.index("--web-origin") :][:2]
    assert "http://127.0.0.1:5173" in argv
    for flag, value in (
        ("--project", str(root)),
        ("--task", "bench"),
        ("--outer-loop", "plain"),
        ("--max-rounds", "4"),
        ("--backend", "cpu"),
        ("--cli-provider", "codex"),
        ("--exp-name", reply["run_id"]),
    ):
        assert argv[argv.index(flag) + 1] == value
    config = tomllib.loads(Path(argv[argv.index("--config") + 1]).read_text())
    assert config["agent"]["roles"]["implementer"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }


def test_start_refuses_a_second_live_run_even_when_racing(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    statuses: list[int] = []

    def start() -> None:
        statuses.append(runs_home.post(f"/api/projects/{key}/runs", START).status)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409]


def test_a_failed_launch_returns_the_stderr_tail(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    runs_home.config.environ = {**runs_home.config.environ, "FAKE_RUN_SERVER_FAIL": "1"}

    reply = runs_home.post(f"/api/projects/{key}/runs", START)

    body = reply.json()
    assert (reply.status, body["error"]["code"]) == (502, "launch_failed")
    assert body["error"]["details"]["stderr_tail"][-1] == "ConfigurationError: bad run"
    log = Path(body["error"]["details"]["stderr_log"])
    assert log.name == "live.stderr.log"
    assert "bad run" in log.read_text()


def test_a_timed_out_launch_is_reaped_and_the_retry_owns_the_gateway(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    runs_home.config.launch_timeout = 1.0
    plain = runs_home.config.environ
    runs_home.config.environ = {**plain, "FAKE_RUN_SERVER_HANG": "1"}

    first = runs_home.post(f"/api/projects/{key}/runs", START).json()
    hung = next(iter(runs_home.config.launches.values())).process
    runs_home.config.environ = plain
    runs_home.config.launch_timeout = 30.0
    second = runs_home.post(f"/api/projects/{key}/runs", START).json()

    assert first["error"]["code"] == "launch_failed"
    assert first["error"]["details"]["stderr_log"].endswith("live.stderr.log")
    assert isinstance(first["error"]["details"]["stderr_tail"], list)
    assert hung.poll() is not None
    gateways = runs_home.config.state_home / "web" / "gateways"
    owner = json.loads(next(gateways.glob("*/live.owner.json")).read_text())
    record = json.loads(next(gateways.glob("*/live.json")).read_text())
    assert (owner["run_id"], owner["pid"]) == (second["run_id"], record["pid"])


def test_the_gateway_accepts_the_app_origins_only(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    gateway = runs_home.post(f"/api/projects/{key}/runs", START).json()["gateway"]
    probe = gateway["websocket_url"].replace("ws://", "http://", 1)

    def status(origin: str) -> int:
        request = urllib.request.Request(probe, headers={"Origin": origin})  # noqa: S310  # lint-waiver: LW-101309 [S310]; probe only the loopback gateway the fake run server published
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # lint-waiver: LW-101310 [S310]; connect only to the loopback request built above
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    assert status(runs_home.config.origin) == 200
    assert status("http://127.0.0.1:5173") == 200
    assert status("http://evil.test") == 403


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"outer_loop": "loop-de-loop"}, "invalid_request"),
        ({"provider": "copilot"}, "unknown_provider"),
        ({"task": "nope"}, "unknown_task"),
        ({"outer_loop": "profile-guided"}, "profile_guided_unavailable"),
        ({"roles": {"implementer": {"model": ""}}}, "invalid_request"),
        ({"budget": 0}, "invalid_request"),
        ({"roles": {"bogus-role": {"model": "m"}}}, "invalid_request"),
    ],
)
def test_start_rejects_bad_requests_before_spawning(
    runs_home: Home, change: dict[str, object], code: str
) -> None:
    key, _ = _project(runs_home)

    reply = runs_home.post(f"/api/projects/{key}/runs", {**START, **change}).json()

    assert reply["error"]["code"] == code
    assert runs_home.config.launches == {}


def test_start_refuses_a_dirty_project(runs_home: Home) -> None:
    key, root = _project(runs_home)
    (root / "scratch.txt").write_text("x")

    reply = runs_home.post(f"/api/projects/{key}/runs", START).json()

    assert reply["error"]["code"] == "dirty_tree"
    assert reply["error"]["details"]["pending"] == ["scratch.txt"]


def test_run_config_is_a_valid_agent_config() -> None:
    body = StartRun.model_validate({**START, "driver": "agentshim", "reasoning_effort": "low"})

    config = Config.model_validate(tomllib.loads(render_run_config(body)))

    assert (config.model.name, config.agent.driver, config.agent.cli_provider) == (
        "gpt-5.5",
        "agentshim",
        "codex",
    )
    assert config.thinking.level == "low"
    assert config.agent.roles["implementer"].reasoning_effort == "high"


def test_a_run_server_that_cannot_start_is_a_typed_launch_failure(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    runs_home.config.run_server_argv = (str(runs_home.workspace / "missing-server"),)

    reply = runs_home.post(f"/api/projects/{key}/runs", START)

    assert (reply.status, reply.json()["error"]["code"]) == (502, "launch_failed")


def test_a_failed_owner_write_kills_the_spawned_child(
    runs_home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, _ = _project(runs_home)
    runs_home.config.environ = {**runs_home.config.environ, "FAKE_RUN_SERVER_HANG": "1"}
    spawned: list[int] = []
    write = runs.atomic_write

    def failing_owner_write(path: Path, data: bytes, *, mode: int) -> None:
        if not path.name.endswith(".owner.json"):
            write(path, data, mode=mode)
            return
        spawned.append(json.loads(data)["pid"])
        raise OSError(28, "No space left on device")

    # test-isolation: a disk-full sidecar write after spawn is not reproducible without faking it.
    monkeypatch.setattr(runs, "atomic_write", failing_owner_write)

    reply = runs_home.post(f"/api/projects/{key}/runs", START)

    assert (reply.status, reply.json()["error"]["code"]) == (502, "launch_failed")
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)
    assert runs_home.config.launches == {}


def test_run_config_escapes_non_bmp_and_control_characters() -> None:
    model = "gpt-\U0001f600-\x7f-\x85"
    body = StartRun.model_validate(
        {**START, "model": model, "roles": {"implementer": {"reasoning_effort": "\U0001f600"}}}
    )

    config = tomllib.loads(render_run_config(body))

    assert config["model"]["name"] == model
    assert config["agent"]["roles"]["implementer"]["reasoning_effort"] == "\U0001f600"


def _persist_run(root: Path, run_id: str, *, max_rounds: int = 3) -> None:
    project = Project.open(root)
    project.state.create_project("proj")
    manifest = project.state.new_run_manifest(
        run_id,
        run_id=run_id,
        branch=f"vibesys-runs/{run_id}",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="plain",
            config_version=1,
            options={
                "max_rounds": max_rounds,
                "max_attempts_per_issue": 2,
                "max_issues_per_perf_eval": 2,
            },
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)


def test_resume_keeps_the_recorded_loop_and_refuses_a_smaller_budget(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run", max_rounds=3)

    smaller = runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {"budget": 2}).json()
    resumed = runs_home.post(f"/api/projects/{key}/runs/plain-run/resume", {"budget": 5}).json()

    argv = _argv(runs_home)
    assert smaller["error"]["code"] == "budget_decrease"
    assert resumed["run_id"] == "plain-run"
    assert argv[argv.index("--resume") + 1] == "plain-run"
    assert argv[argv.index("--outer-loop") + 1] == "plain"
    assert argv[argv.index("--max-rounds") + 1] == "5"
    missing = runs_home.post(f"/api/projects/{key}/runs/ghost/resume", {}).json()
    assert missing["error"]["code"] == "unknown_run"


def test_open_serves_a_run_read_only_beside_the_live_one(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")

    first = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
    again = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()

    argv = _argv(runs_home, "reopen-plain-run")
    assert first["gateway"]["state"] == "reopened"
    assert again["gateway"]["url"] == first["gateway"]["url"]
    assert argv[argv.index("--web-reopen-run") + 1] == "plain-run"
    assert argv[argv.index("--project") + 1] == str(root)
    assert "--web-reopen" not in argv
    missing = runs_home.post(f"/api/projects/{key}/runs/ghost/open")
    assert (missing.status, missing.json()["error"]["code"]) == (404, "unknown_run")


@contextlib.contextmanager
def _external_gateway(
    record: Path, cwd: Path, *arguments: str
) -> Iterator[subprocess.Popen[bytes]]:
    """Run the fake gateway in the test's own process group, as another launcher would."""
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101311 [S603]; the test starts its own fake gateway script with fixed arguments.
        # > run_test_command waits for exit, but this gateway must keep serving while the
        # > test queries the API; a shell wrapper would add quoting for no gain.
        [sys.executable, str(FAKE), "--web-instance", str(record), *arguments],
        cwd=cwd,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        yield process
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_a_reopen_published_by_another_launcher_is_reused(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    record = Project.open(root).configuration_path() / "web-gateway-plain-run.json"
    with _external_gateway(record, root, "--web-reopen-run", "plain-run"):
        opened = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
        assert opened["gateway"]["url"] == json.loads(record.read_text())["url"]
        assert runs_home.config.launches == {}


def test_a_stale_reopen_record_is_replaced_by_a_new_gateway(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    first = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
    old = next(iter(runs_home.config.launches.values())).process
    record = next(
        (runs_home.config.state_home / "web" / "gateways").glob("*/reopen-plain-run.json")
    )
    stale = record.read_bytes()
    old.kill()
    old.wait(timeout=10)
    record.write_bytes(stale)

    again = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()

    assert again["gateway"]["state"] == "reopened"
    assert again["gateway"]["url"] != first["gateway"]["url"]
    assert json.loads(record.read_text())["pid"] != old.pid


def test_a_reopen_for_an_old_home_origin_is_restarted(runs_home: Home) -> None:
    key, root = _project(runs_home)
    _persist_run(root, "plain-run")
    first = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()
    old = next(iter(runs_home.config.launches.values())).process
    owner = next(
        (runs_home.config.state_home / "web" / "gateways").glob("*/reopen-plain-run.owner.json")
    )
    owner.write_text(json.dumps({**json.loads(owner.read_text()), "origin": "http://127.0.0.1:1"}))

    again = runs_home.post(f"/api/projects/{key}/runs/plain-run/open").json()

    assert old.wait(timeout=10) is not None
    assert again["gateway"]["url"] != first["gateway"]["url"]
    assert json.loads(owner.read_text())["origin"] == runs_home.config.origin


def test_stop_sends_an_external_gateway_a_plain_sigterm(runs_home: Home) -> None:
    key, root = _project(runs_home)
    record = Project.open(root).configuration_path() / "web-gateway.json"
    # Not a session leader: killpg(pid) would find no such group and signal nothing.
    with _external_gateway(record, root) as process:
        stopped = runs_home.delete(f"/api/projects/{key}/live").json()

        assert stopped["stopped"] is True
        assert process.wait(timeout=10) == 0


def test_stop_terminates_the_live_gateway_once(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    started = runs_home.post(f"/api/projects/{key}/runs", START).json()

    stopped = runs_home.delete(f"/api/projects/{key}/live").json()
    next(iter(runs_home.config.launches.values())).process.wait(timeout=10)

    assert stopped == {"stopped": True, "run_id": started["run_id"]}
    assert runs_home.delete(f"/api/projects/{key}/live").json() == {
        "stopped": False,
        "run_id": None,
    }


def test_resume_and_open_reuse_the_live_gateway_of_the_same_run(runs_home: Home) -> None:
    key, _ = _project(runs_home)
    started = runs_home.post(f"/api/projects/{key}/runs", START).json()
    run_id = started["run_id"]

    resumed = runs_home.post(f"/api/projects/{key}/runs/{run_id}/resume", {}).json()
    opened = runs_home.post(f"/api/projects/{key}/runs/{run_id}/open").json()

    assert resumed["gateway"]["url"] == started["gateway"]["url"]
    assert opened["gateway"] == {**resumed["gateway"], "state": "live"}
    assert len(runs_home.config.launches) == 1


def test_a_new_launch_prunes_launches_that_have_exited(runs_home: Home) -> None:
    key, root = _project(runs_home)
    plain = runs_home.config.environ
    runs_home.config.environ = {**plain, "FAKE_RUN_SERVER_FAIL": "1"}
    assert runs_home.post(f"/api/projects/{key}/runs", START).status == 502
    runs_home.config.environ = plain
    _persist_run(root, "plain-run")

    runs_home.post(f"/api/projects/{key}/runs/plain-run/open")

    assert [path.name for path in runs_home.config.launches] == ["reopen-plain-run.json"]
