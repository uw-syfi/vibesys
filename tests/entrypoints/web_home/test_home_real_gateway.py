"""The home server against the real run server: token and Origin compatibility.

Needs sub-project 1 (`--web-reopen-run`, `tests/server/support.finished_run`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.web_home.support import project_key
from tests.server.support import finished_run
from tests.support import run_test_command
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect
from websockets.typing import Origin

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_a_real_reopen_gateway_accepts_only_the_app_origin_and_token(home: Home) -> None:
    project, run_id, _log_dir = finished_run(home.workspace / "proj")
    run_test_command(["git", "init", "-q"], cwd=project.root, check=True)
    key = project_key(home, project.root)
    try:
        reply = home.post(f"/api/projects/{key}/runs/{run_id}/open")
        gateway = reply.json()["gateway"]
        url = gateway["websocket_url"]

        with connect(url, origin=Origin(home.config.origin), open_timeout=10):
            pass
        with connect(url, origin=Origin("http://127.0.0.1:5173"), open_timeout=10):
            pass
        with pytest.raises(InvalidStatus), connect(url, origin=Origin("http://evil.test")):
            pass
        forged = url.replace(gateway["token"], "wrong")
        with pytest.raises(InvalidStatus), connect(forged, origin=Origin(home.config.origin)):
            pass
        assert reply.status == 200
    finally:
        for launch in home.config.launches.values():
            launch.process.terminate()
            launch.process.wait(timeout=30)
