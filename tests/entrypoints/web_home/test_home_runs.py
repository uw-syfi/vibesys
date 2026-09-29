from __future__ import annotations

import json
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.web_home.support import make_project, project_key

from entrypoints.web_home.contract import StartRun
from entrypoints.web_home.runs import render_run_config
from vibesys.api import Config

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
